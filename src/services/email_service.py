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

    def send_account_deletion_code(self, to_email: str, code: str) -> None:
        """O texto diz EXCLUIR, e isso nao e detalhe de redacao.

        O mesmo codigo de seis digitos serve a tres pedidos diferentes neste
        sistema, e o e-mail e o unico lugar onde a pessoa ve o que ela esta
        confirmando. Um "seu codigo de verificacao" chegando para uma exclusao
        IRREVERSIVEL faria alguem digitar sem saber o que estava aceitando.
        """
        self._send_email(
            to_email=to_email,
            subject="Código para excluir sua conta",
            body=(
                f"Seu código para EXCLUIR a conta é: {code}\n\n"
                "Ele expira em 10 minutos.\n"
                "A exclusão não tem desfazer: seus pedidos, endereços e saldo "
                "de cashback deixam de estar ligados a você.\n\n"
                "Se você não pediu isso, ignore este e-mail — nada será "
                "excluído."
            ),
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
