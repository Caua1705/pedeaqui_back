from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.schemas.auth_schema import (
    ForgotPasswordRequest,
    LoginRequest,
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
from src.services.auth_service import AuthService


router = APIRouter(prefix="/auth", tags=["customer auth"])


@router.post("/register", response_model=RegisterCustomerResponse)
def register_customer(payload: RegisterCustomerRequest, db: Session = Depends(get_db)) -> RegisterCustomerResponse:
    return AuthService(db).register(payload)


@router.post("/verify-email-code", response_model=VerifyEmailCodeResponse)
def verify_email_code(payload: VerifyEmailCodeRequest, db: Session = Depends(get_db)) -> VerifyEmailCodeResponse:
    return AuthService(db).verify_email_code(payload)


@router.post("/resend-email-code", response_model=MessageResponse)
def resend_email_code(payload: ResendEmailCodeRequest, db: Session = Depends(get_db)) -> MessageResponse:
    return AuthService(db).resend_email_code(payload)


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    return AuthService(db).login(payload)


@router.post("/forgot-password", response_model=MessageResponse)
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)) -> MessageResponse:
    return AuthService(db).forgot_password(payload)


@router.post("/verify-reset-code", response_model=VerifyResetCodeResponse)
def verify_reset_code(payload: VerifyResetCodeRequest, db: Session = Depends(get_db)) -> VerifyResetCodeResponse:
    return AuthService(db).verify_reset_code(payload)


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> MessageResponse:
    return AuthService(db).reset_password(payload)
