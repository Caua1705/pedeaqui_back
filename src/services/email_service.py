import resend
from fastapi import HTTPException, status

from src.core.config import settings


class EmailService:
    def send_email_verification_code(self, to_email: str, code: str) -> None:
        self._send_email(
            to_email=to_email,
            subject="Seu código de verificação",
            body=f"Seu código de verificação é: {code}\n\nEle expira em 10 minutos.",
        )

    def send_password_reset_code(self, to_email: str, code: str) -> None:
        self._send_email(
            to_email=to_email,
            subject="Código para redefinir sua senha",
            body=(
                f"Seu código de recuperação de senha é: {code}\n\n"
                "Ele expira em 10 minutos.\n"
                "Se você não solicitou isso, ignore este e-mail."
            ),
        )

    def _send_email(self, to_email: str, subject: str, body: str) -> None:
        if not settings.RESEND_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="RESEND_API_KEY is not configured",
            )

        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send(
            {
                "from": settings.EMAIL_FROM,
                "to": [to_email],
                "subject": subject,
                "text": body,
            }
        )
