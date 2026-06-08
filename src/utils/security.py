import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from passlib.context import CryptContext

from src.core.config import settings


_PASSWORD_ITERATIONS = 390_000
_BCRYPT_MAX_PASSWORD_BYTES = 72
_PASSWORD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")


class TokenExpiredError(Exception):
    pass


class TokenInvalidError(Exception):
    pass


class PasswordTooLongError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > _BCRYPT_MAX_PASSWORD_BYTES:
        raise PasswordTooLongError("Password is too long")
    return _PASSWORD_CONTEXT.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    if password_hash.startswith("$2"):
        try:
            return _PASSWORD_CONTEXT.verify(password, password_hash)
        except (ValueError, TypeError):
            return False
    try:
        algorithm, iterations, salt, expected = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _b64decode(salt), int(iterations))
        return hmac.compare_digest(_b64encode(digest), expected)
    except (ValueError, TypeError):
        return False


def generate_6_digit_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def generate_numeric_code() -> str:
    return generate_6_digit_code()


def hash_verification_code(code: str) -> str:
    return _hmac_hex(code, settings.EMAIL_CODE_SECRET)


def verify_verification_code(code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_verification_code(code), code_hash)


def generate_reset_token() -> str:
    return secrets.token_urlsafe(32)


def hash_reset_token(token: str) -> str:
    return _hmac_hex(token, settings.PASSWORD_RESET_SECRET)


def verify_reset_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_reset_token(token), token_hash)


def hash_code(code: str) -> str:
    return hash_verification_code(code)


def verify_code(code: str, code_hash: str) -> bool:
    return verify_verification_code(code, code_hash)


def create_signed_token(subject: str, purpose: str, expires_delta: timedelta, extra: dict[str, Any] | None = None) -> str:
    now = utcnow()
    payload: dict[str, Any] = {
        "sub": subject,
        "purpose": purpose,
        "iat": now,
        "exp": now + expires_delta,
    }
    if extra:
        payload.update(extra)

    return jwt.encode(payload, _customer_auth_secret(), algorithm="HS256")


def decode_signed_token(token: str, purpose: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, _customer_auth_secret(), algorithms=["HS256"])
        if payload.get("purpose") != purpose:
            raise TokenInvalidError
        return payload
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError from exc
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        raise TokenInvalidError from exc


def _customer_auth_secret() -> str:
    secret = settings.CUSTOMER_AUTH_SECRET or settings.CUSTOMER_JWT_SECRET
    if not secret:
        raise TokenInvalidError("Customer auth secret is not configured")
    return secret


def _hmac_hex(value: str, secret: str | None) -> str:
    key = (secret or settings.CUSTOMER_AUTH_SECRET or settings.INTERNAL_API_KEY).encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
