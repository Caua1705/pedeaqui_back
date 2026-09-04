import resend
from fastapi import HTTPException, status

from src.core.config import settings


class EmailService:
    """Os quatro e-mails de código, e a regra que vale para o quinto.

    Um código de seis dígitos serve a QUATRO pedidos diferentes neste sistema —
    entrar, ligar a conta do Google, redefinir a senha, excluir a conta — e o
    e-mail é o único lugar onde a pessoa vê **o que ela está confirmando**. A
    tela que pede o código é a que ela já está olhando; o e-mail é a única
    coisa que pode contradizê-la.

    Por isso cada propósito tem texto próprio, e o texto nomeia a AÇÃO no
    assunto e na primeira linha. Não é redação: é o que faz alguém que pediu um
    código para entrar parar antes de digitar num formulário que faz outra
    coisa.

    **A separação é do TEXTO, não da máquina.** Entrar e ligar o Google
    compartilham tabela, HMAC, TTL, cooldown e teto de reenvio de propósito —
    ver `AuthService.send_link_confirmation_code`. Dois tipos de código seriam
    duas janelas, dois tetos e duas retenções para envelhecer separado.

    O resíduo que isso deixa, e é melhor sabê-lo do que descobri-lo: como a
    linha é a mesma, um código emitido com o texto de "entrar" **funciona** na
    confirmação do Google, e vice-versa. O texto diz o que o sistema esperava
    que ele fizesse, não o que ele é capaz de fazer. Fechar isso exigiria o
    segundo tipo de código que o parágrafo acima recusa.
    """

    def send_email_verification_code(self, to_email: str, code: str) -> None:
        """O código do cadastro por e-mail: confirmar o endereço e entrar."""
        self._send_email(
            to_email=to_email,
            subject="Código para entrar na sua conta",
            body=(
                f"Seu código para ENTRAR é: {code}\n\n"
                "Ele confirma que este e-mail é seu e libera o acesso à sua "
                "conta.\n"
                "Ele expira em 10 minutos.\n\n"
                "Se você não pediu isso, ignore este e-mail."
            ),
        )

    def send_google_link_code(self, to_email: str, code: str) -> None:
        """O código do caso (b) do "entrar com Google".

        Ele nao e o de entrar, e dizer "codigo de verificacao" nos dois era o
        buraco: quem pediu um codigo para entrar e recebe uma tela de LIGAR
        outra conta a sua clica em confirmar por habito. O e-mail e onde a
        pessoa tem chance de reparar que sao coisas diferentes.

        E o que este codigo autoriza NAO e pequeno: a partir dele, entrar com
        aquela conta do Google passa a abrir ESTA conta, com os pedidos, os
        enderecos e o saldo dentro.
        """
        self._send_email(
            to_email=to_email,
            subject="Código para ligar sua conta do Google",
            body=(
                f"Seu código para LIGAR sua conta do Google é: {code}\n\n"
                "Alguém entrou com uma conta do Google que usa este e-mail. "
                "Confirmando este código, essa conta do Google passa a abrir a "
                "sua conta aqui.\n"
                "Ele expira em 10 minutos.\n\n"
                "Se não foi você, NÃO use este código — e ninguém entra na sua "
                "conta com ele."
            ),
        )

    def send_account_deletion_code(self, to_email: str, code: str) -> None:
        """O texto diz EXCLUIR, e isso nao e detalhe de redacao.

        Foi o primeiro dos quatro a nomear a acao, e o motivo esta no docstring
        da classe: um "seu codigo de verificacao" chegando para uma exclusao
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
