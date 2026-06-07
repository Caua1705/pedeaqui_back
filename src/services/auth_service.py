import uuid
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.customer_model import Customer, PasswordResetCode
from src.repositories.customer_repository import CustomerRepository
from src.schemas.auth_schema import (
    ForgotPasswordRequest,
    LoginRequest,
    LoginCustomerResponse,
    LoginResponse,
    MessageResponse,
    RegisterCustomerRequest,
    RegisterCustomerResponse,
    ResetPasswordRequest,
    ResendEmailCodeRequest,
    VerifyEmailCodeRequest,
    VerifyEmailCodeResponse,
    VerifyResetCodeRequest,
    VerifyResetCodeResponse,
)
from src.services.email_service import EmailService
from src.utils.normalization import is_valid_cpf, is_valid_email, normalize_digits, normalize_email
from src.utils.security import (
    create_signed_token,
    decode_signed_token,
    generate_6_digit_code,
    generate_reset_token,
    hash_reset_token,
    hash_verification_code,
    hash_password,
    utcnow,
    verify_password,
    verify_reset_token,
    verify_verification_code,
)


MAX_CODE_ATTEMPTS = 5
MAX_RESENDS = 3
RESEND_COOLDOWN_SECONDS = 60
CODE_TTL_MINUTES = 10
RESEND_WINDOW_MINUTES = 15


class AuthService:
    def __init__(self, db: Session):
        self.db = db
        self.customer_repository = CustomerRepository(db)
        self.email_service = EmailService()

    def register(self, payload: RegisterCustomerRequest) -> RegisterCustomerResponse:
        email = normalize_email(payload.email)
        phone = normalize_digits(payload.phone)
        cpf = normalize_digits(payload.cpf)

        if not is_valid_email(email):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email invalido")
        if not is_valid_cpf(cpf):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CPF invalido")
        if len(payload.password) < 8:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Senha fraca")
        if not payload.privacy_accepted:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Aceite de privacidade obrigatorio")

        conflict = self._registration_conflict(email=email, cpf=cpf, phone=phone)
        if conflict:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{conflict} ja cadastrado")

        try:
            customer = self.customer_repository.create(
                name=payload.name.strip(),
                email=email,
                phone=phone,
                cpf=cpf,
                password_hash=hash_password(payload.password),
                birth_date=payload.birth_date,
                email_verified_at=None,
                phone_verified_at=None,
                marketing_opt_in=payload.marketing_opt_in,
                privacy_accepted_at=utcnow(),
                is_active=True,
            )
            self._create_email_verification_code(customer)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return RegisterCustomerResponse(
            customer_id=customer.id,
            email=customer.email,
            requires_email_verification=True,
            message="Codigo de verificacao enviado para seu e-mail.",
        )

    def verify_email_code(self, payload: VerifyEmailCodeRequest) -> VerifyEmailCodeResponse:
        email = normalize_email(payload.email)
        customer = self.customer_repository.get_by_email(email)
        if not customer:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cliente nao encontrado")

        code_row = self.customer_repository.latest_unused_email_code(email)
        if not code_row:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Codigo expirado")

        if code_row.attempts_count >= MAX_CODE_ATTEMPTS:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Muitas tentativas")
        if code_row.expires_at < utcnow():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Codigo expirado")

        if not verify_verification_code(payload.code, code_row.code_hash):
            code_row.attempts_count += 1
            self.db.add(code_row)
            self.db.commit()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Codigo invalido")

        customer.email_verified_at = utcnow()
        code_row.used_at = utcnow()
        self.db.add(customer)
        self.db.add(code_row)
        self.db.commit()
        return VerifyEmailCodeResponse(verified=True, message="E-mail verificado com sucesso.")

    def resend_email_code(self, payload: ResendEmailCodeRequest) -> MessageResponse:
        email = normalize_email(payload.email)
        customer = self.customer_repository.get_by_email(email)
        if customer and not customer.email_verified_at:
            latest = self.customer_repository.latest_unused_email_code(email)
            if latest and latest.created_at and (utcnow() - latest.created_at).total_seconds() < RESEND_COOLDOWN_SECONDS:
                return MessageResponse(message="Se este e-mail estiver cadastrado, enviaremos um codigo de verificacao.")
            recent_count = self.customer_repository.count_email_codes_since(
                email=email,
                since=utcnow() - timedelta(minutes=RESEND_WINDOW_MINUTES),
            )
            if recent_count < MAX_RESENDS:
                resend_count = (latest.resend_count + 1) if latest else 0
                self._create_email_verification_code(customer, resend_count=resend_count)
                self.db.commit()

        return MessageResponse(message="Se este e-mail estiver cadastrado, enviaremos um codigo de verificacao.")

    def login(self, payload: LoginRequest) -> LoginResponse:
        identifier = payload.login.strip()
        email = normalize_email(identifier) if "@" in identifier else None
        phone = normalize_digits(identifier) if "@" not in identifier else None
        customer = self.customer_repository.get_by_email_or_phone(email=email, phone=phone)

        if not customer or not verify_password(payload.password, customer.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais invalidas")
        if not customer.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa")
        if not customer.email_verified_at:
            return LoginResponse(
                requires_email_verification=True,
                email=customer.email,
                message="Verifique seu e-mail para continuar.",
            )

        token = create_signed_token(
            subject=str(customer.id),
            purpose="customer_access",
            expires_delta=timedelta(minutes=settings.CUSTOMER_ACCESS_TOKEN_MINUTES),
            extra={"type": "customer"},
        )
        return LoginResponse(
            access_token=token,
            token_type="bearer",
            customer=LoginCustomerResponse(
                id=customer.id,
                name=customer.name,
                email=customer.email,
                phone=customer.phone,
                email_verified=True,
            ),
        )

    def forgot_password(self, payload: ForgotPasswordRequest) -> MessageResponse:
        email = normalize_email(payload.email)
        customer = self.customer_repository.get_by_email(email)
        if customer:
            latest = self.customer_repository.latest_unused_password_reset_code(email)
            if latest and latest.created_at and (utcnow() - latest.created_at).total_seconds() < RESEND_COOLDOWN_SECONDS:
                return MessageResponse(message="Se este e-mail estiver cadastrado, enviaremos um codigo de recuperacao.")
            recent_count = self.customer_repository.count_password_reset_codes_since(
                email=email,
                since=utcnow() - timedelta(minutes=RESEND_WINDOW_MINUTES),
            )
            if recent_count < MAX_RESENDS:
                self._create_password_reset_code(customer)
                self.db.commit()
        return MessageResponse(message="Se este e-mail estiver cadastrado, enviaremos um codigo de recuperacao.")

    def verify_reset_code(self, payload: VerifyResetCodeRequest) -> VerifyResetCodeResponse:
        code_row = self.customer_repository.latest_unused_password_reset_code(normalize_email(payload.email))
        if not code_row:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Codigo invalido ou expirado")
        if code_row.attempts_count >= MAX_CODE_ATTEMPTS:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Muitas tentativas")
        if code_row.expires_at < utcnow():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Codigo expirado")

        if not verify_verification_code(payload.code, code_row.code_hash):
            code_row.attempts_count += 1
            self.db.add(code_row)
            self.db.commit()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Codigo invalido")

        reset_token = generate_reset_token()
        code_row.reset_token_hash = hash_reset_token(reset_token)
        code_row.reset_token_expires_at = utcnow() + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_MINUTES)
        self.db.add(code_row)
        self.db.commit()
        return VerifyResetCodeResponse(reset_token=reset_token)

    def reset_password(self, payload: ResetPasswordRequest) -> MessageResponse:
        if payload.new_password != payload.confirm_password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Confirmacao de senha nao confere")
        if len(payload.new_password) < 8:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Senha fraca")

        code_row = self.customer_repository.get_password_reset_by_token_hash(hash_reset_token(payload.reset_token))
        if not self._valid_reset_token(code_row, payload.reset_token):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token invalido ou expirado")

        customer = self.customer_repository.get_by_id(code_row.customer_id)
        if not customer:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token invalido ou expirado")

        customer.password_hash = hash_password(payload.new_password)
        code_row.used_at = utcnow()
        self.customer_repository.invalidate_unused_password_reset_codes(customer.id)
        self.db.add(customer)
        self.db.add(code_row)
        self.db.commit()
        return MessageResponse(message="Senha alterada com sucesso.")

    def get_customer_from_token(self, token: str) -> Customer | None:
        payload = decode_signed_token(token, "customer_access")
        if not payload or payload.get("type") != "customer":
            return None
        try:
            return self.customer_repository.get_by_id(uuid.UUID(payload["sub"]))
        except (ValueError, KeyError):
            return None

    def _registration_conflict(self, email: str, cpf: str, phone: str) -> str | None:
        if self.customer_repository.get_by_email(email):
            return "Email"
        if self.customer_repository.get_by_cpf(cpf):
            return "CPF"
        if self.customer_repository.get_by_phone(phone):
            return "Telefone"
        return None

    def _create_email_verification_code(self, customer: Customer, resend_count: int = 0) -> None:
        code = generate_6_digit_code()
        self.customer_repository.create_email_code(
            customer_id=customer.id,
            email=customer.email,
            code_hash=hash_verification_code(code),
            expires_at=utcnow() + timedelta(minutes=CODE_TTL_MINUTES),
            attempts_count=0,
            resend_count=resend_count,
        )
        self.email_service.send_email_verification_code(customer.email, code)

    def _create_password_reset_code(self, customer: Customer) -> None:
        code = generate_6_digit_code()
        self.customer_repository.create_password_reset_code(
            customer_id=customer.id,
            email=customer.email,
            code_hash=hash_verification_code(code),
            expires_at=utcnow() + timedelta(minutes=CODE_TTL_MINUTES),
            attempts_count=0,
            resend_count=0,
        )
        self.email_service.send_password_reset_code(customer.email, code)

    @staticmethod
    def _valid_reset_token(code_row: PasswordResetCode | None, reset_token: str) -> bool:
        if not code_row or code_row.used_at or not code_row.reset_token_hash or not code_row.reset_token_expires_at:
            return False
        if code_row.reset_token_expires_at < utcnow():
            return False
        return verify_reset_token(reset_token, code_row.reset_token_hash)
