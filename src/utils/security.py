import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core.config import settings


_PASSWORD_ITERATIONS = 390_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
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
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    if extra:
        payload.update(extra)

    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_json_b64(header)}.{_json_b64(payload)}"
    signature = hmac.new(_secret(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64encode(signature)}"


def decode_signed_token(token: str, purpose: str) -> dict[str, Any] | None:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".", 2)
        signing_input = f"{header_b64}.{payload_b64}"
        expected = hmac.new(_secret(), signing_input.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64encode(expected), signature_b64):
            return None
        payload = json.loads(_b64decode(payload_b64))
        if payload.get("purpose") != purpose:
            return None
        if int(payload.get("exp", 0)) < int(utcnow().timestamp()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _secret() -> bytes:
    return (settings.CUSTOMER_AUTH_SECRET or settings.INTERNAL_API_KEY).encode("utf-8")


def _hmac_hex(value: str, secret: str | None) -> str:
    key = (secret or settings.CUSTOMER_AUTH_SECRET or settings.INTERNAL_API_KEY).encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _json_b64(value: dict[str, Any]) -> str:
    return _b64encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
