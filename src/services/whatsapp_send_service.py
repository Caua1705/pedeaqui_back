"""Mandar mensagem por um canal: quem decide entre texto livre e template.

## A janela de 24h, e por que a decisao mora AQUI

A Meta so aceita texto livre enquanto o cliente tiver escrito para aquele
numero nas ultimas 24 horas. Fora dela, so template aprovado.

A regra podia morar em quem chama — e nao pode, pelo motivo da armadilha 46:
regra que depende de alguem lembrar de aplica-la e uma recomendacao. Um
chamador novo que mandasse texto livre fora da janela receberia `131047` da
Meta, e o sintoma seria um cliente **nao avisado**, sem erro nosso em lugar
nenhum. Aqui a janela e conferida antes de a chamada existir.

**Os avisos de pedido sao SEMPRE template**, e nao por conservadorismo: o
cliente pediu pelo app e nunca escreveu no WhatsApp da loja, entao a janela
esta fechada em praticamente 100% dos pedidos.

## O telefone e convertido, e a conversao nao inventa

`normalize_digits` deixa so digitos, e a armadilha 27 registra o residuo:
`+55 85 9...` vira `5585...` e `85 9...` vira `859...`. Os dois sao o mesmo
telefone escrito diferente, e o segundo nao tem DDI.

A regra e fechada, e o que nao casa **nao e enviado**: chutar um DDI e mandar
a mensagem de um cliente para o telefone de outra pessoa.

## O telefone nunca vai para o log

Nem no caminho de erro. Quem identifica o envio no log e o pedido e o
`wamid` — a mesma regra que ja tira e-mail de pagador e mensagem de chat das
linhas de log.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.integrations.whatsapp_client import send_template_message, send_text_message
from src.models.whatsapp_model import WhatsAppChannel
from src.repositories.whatsapp_repository import WhatsAppContactWindowRepository
from src.utils.crypto import decrypt_whatsapp_token
from src.utils.normalization import normalize_digits


logger = logging.getLogger("uvicorn.error")

DDI_DO_BRASIL = "55"

# Com DDD e sem DDI: fixo (10) e celular com o nono digito (11).
TAMANHOS_SEM_DDI = (10, 11)
# Os mesmos, com o DDI na frente.
TAMANHOS_COM_DDI = (12, 13)


class WhatsAppSendRefused(Exception):
    """A mensagem NAO foi mandada, e a decisao foi nossa.

    Separada dos erros do cliente da Meta de proposito: aqui a chamada nem
    aconteceu. `reason` diz qual das duas recusas foi, porque elas pedem
    coisas diferentes de quem le o log — `phone` e cadastro do cliente,
    `window` e o texto livre que devia ser template.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def to_whatsapp_phone(phone: str | None) -> str | None:
    """O telefone do pedido no formato que a Meta usa: digitos com DDI, sem `+`.

    `None` quer dizer "nao da para afirmar qual numero e este", e o chamador
    NAO envia. As quatro formas aceitas cobrem o que o checkout produz; o que
    sobra sao numeros que precisariam de um chute para virar E.164.
    """
    digitos = normalize_digits(phone)
    if len(digitos) in TAMANHOS_SEM_DDI:
        return f"{DDI_DO_BRASIL}{digitos}"
    if len(digitos) in TAMANHOS_COM_DDI and digitos.startswith(DDI_DO_BRASIL):
        return digitos
    return None


class WhatsAppSender:
    """O mecanismo de envio. Nao decide O QUE mandar — decide se PODE."""

    def __init__(self, db: Session):
        self.db = db
        self.window_repository = WhatsAppContactWindowRepository(db)
        # Injetavel, pela convencao da armadilha 51: o teste declara o
        # instante em vez de depender da hora em que roda.
        self.clock = lambda: datetime.now(timezone.utc)

    def send_template(
        self,
        *,
        channel: WhatsAppChannel,
        to_phone: str | None,
        template_name: str,
        language: str,
        parameters: tuple[str, ...],
    ) -> str:
        """Manda um template aprovado. Devolve o `wamid`.

        Nao consulta a janela porque template nao depende dela — e e por isso
        que ele e o caminho dos avisos de pedido.
        """
        destino = self._destino(to_phone)
        return send_template_message(
            access_token=decrypt_whatsapp_token(channel.access_token_encrypted),
            phone_number_id=channel.phone_number_id,
            to=destino,
            template_name=template_name,
            language=language,
            parameters=parameters,
        )

    def send_text(
        self,
        *,
        channel: WhatsAppChannel,
        to_phone: str | None,
        body: str,
        now: datetime | None = None,
    ) -> str:
        """Manda texto livre. Devolve o `wamid`.

        Recusa fora da janela em vez de tentar e falhar: a tentativa custaria
        um `131047` da Meta e o mesmo cliente nao avisado, com a diferenca de
        que o motivo so apareceria na resposta dela.
        """
        destino = self._destino(to_phone)
        if not self.window_repository.is_open(
            channel_id=channel.id, phone_e164=destino, now=now or self.clock()
        ):
            raise WhatsAppSendRefused(
                "Fora da janela de 24h: texto livre nao e aceito, use um template.",
                reason="window",
            )
        return send_text_message(
            access_token=decrypt_whatsapp_token(channel.access_token_encrypted),
            phone_number_id=channel.phone_number_id,
            to=destino,
            body=body,
        )

    @staticmethod
    def _destino(to_phone: str | None) -> str:
        destino = to_whatsapp_phone(to_phone)
        if destino is None:
            raise WhatsAppSendRefused(
                "Telefone do pedido nao vira E.164 sem chute: nada foi enviado.",
                reason="phone",
            )
        return destino
