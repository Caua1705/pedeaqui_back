from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from src.utils.normalization import normalize_digits, normalize_email


class RegisterCustomerRequest(BaseModel):
    name: str = Field(min_length=1)
    email: str
    phone: str = Field(min_length=8)
    birth_date: date
    cpf: str = Field(min_length=11)
    password: str
    marketing_opt_in: bool = False
    privacy_accepted: bool

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("phone", "cpf")
    @classmethod
    def normalize_digits_value(cls, value: str) -> str:
        return normalize_digits(value)


class RegisterCustomerResponse(BaseModel):
    customer_id: UUID
    email: str
    requires_email_verification: bool
    message: str


class VerifyEmailCodeRequest(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, value: str) -> str:
        return normalize_email(value)


class ResendEmailCodeRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, value: str) -> str:
        return normalize_email(value)


class LoginRequest(BaseModel):
    login: str
    password: str


class LoginCustomerResponse(BaseModel):
    id: UUID
    name: str
    email: str
    phone: str
    email_verified: bool


class LoginResponse(BaseModel):
    access_token: str | None = None
    token_type: str | None = None
    customer: LoginCustomerResponse | None = None
    requires_email_verification: bool = False
    email: str | None = None
    message: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, value: str) -> str:
        return normalize_email(value)


class VerifyResetCodeRequest(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, value: str) -> str:
        return normalize_email(value)


class VerifyResetCodeResponse(BaseModel):
    reset_token: str


class ResetPasswordRequest(BaseModel):
    reset_token: str
    new_password: str
    confirm_password: str


class MessageResponse(BaseModel):
    message: str


class VerifyEmailCodeResponse(BaseModel):
    verified: bool
    message: str


class CustomerSession(BaseModel):
    id: UUID
    name: str
    email: str
    phone: str
    email_verified_at: datetime | None = None
    is_active: bool
