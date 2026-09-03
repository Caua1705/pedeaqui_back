"""Os avisos de status do pedido pelo WhatsApp: aceito, saiu, entregue.

## Sempre TEMPLATE, e nao por conservadorismo

O cliente pediu pelo app e nunca escreveu no WhatsApp da loja: a janela de 24h
esta fechada em praticamente 100% dos pedidos. Texto livre ali volta `131047`
da Meta e o cliente **nao e avisado** — o pior desfecho possivel para uma
funcionalidade cujo valor inteiro e o cliente saber o que esta acontecendo.

## O nome do aviso NAO e o nome do template

`kind` (`order_accepted`) e nosso e esta no CHECK do banco;
`template_name` (`pedido_aceito`) e o nome aprovado na Meta e pode ser
renomeado, reaprovado ou trocado por outro idioma sem que o aviso deixe de ser
"o pedido foi aceito". `_TEMPLATE_POR_KIND` e a unica costura entre os dois.

## A conferencia ANTES do envio nao e redundante com o UNIQUE

`uq_whatsapp_messages_order_kind` barra a LINHA — e nesse ponto a mensagem JA
SAIU. O `UNIQUE` protege a tabela; quem protege o cliente de receber duas
mensagens e `exists_for`, antes da chamada.

## Falha de envio nao vira excecao para quem chama

A mudanca de status ja esta gravada quando este service roda. O que da errado
aqui vira uma LINHA `failed` — que e o registro de que o cliente nao foi
avisado, e o unico lugar onde isso e visivel depois. Quem le essa tabela sabe
o que aconteceu; quem le so o log precisa saber que ela existe.

As recusas NOSSAS (telefone que nao vira E.164, janela fechada) entram com
`refused:<motivo>` para nao se confundirem com o codigo numerico da Meta. Sao
dois vocabularios, e misturá-los faria um `132001` e um "telefone torto"
parecerem o mesmo tipo de problema.

O que este service NAO trata e o inesperado: isso sobe para
`OrderStatusChangeService`, que tem o `except` largo pelo mesmo motivo do
estorno — o aceite ja aconteceu, e virar 500 diria ao lojista que nao.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from src.core.config import settings
from src.integrations.whatsapp_client import WhatsAppSendError
from src.models.whatsapp_model import WhatsAppChannel, WhatsAppMessage
from src.repositories.restaurant_repository import RestaurantRepository
from src.repositories.whatsapp_repository import (
    WhatsAppChannelRepository,
    WhatsAppMessageRepository,
)
from src.services.whatsapp_send_service import WhatsAppSendRefused, WhatsAppSender


logger = logging.getLogger("uvicorn.error")

# Status do pedido -> aviso. So estes tres avisam; o resto passa em silencio.
_KIND_POR_STATUS = {
    "accepted": "order_accepted",
    "out_for_delivery": "order_out_for_delivery",
    "completed": "order_delivered",
}

# Aviso -> nome do template aprovado na Meta.
_TEMPLATE_POR_KIND = {
    "order_accepted": "pedido_aceito",
    "order_out_for_delivery": "pedido_saiu_para_entrega",
    "order_delivered": "pedido_entregue",
}

IDIOMA_DO_TEMPLATE = "pt_BR"

KIND_ENTREGUE = "order_delivered"

# Os tipos de pedido em que "foi entregue" e verdade. Lista positiva de
# proposito (armadilha 47): tipo novo NAO recebe o aviso de entrega ate
# alguem decidir que recebe, que e o lado que fecha — o texto do template
# afirma uma coisa que pode nao ter acontecido.
ORDER_TYPES_QUE_SAO_ENTREGUES = ("delivery",)


class WhatsAppOrderNotifier:
    def __init__(self, db: Session):
        self.db = db
        self.channel_repository = WhatsAppChannelRepository(db)
        self.message_repository = WhatsAppMessageRepository(db)
        self.restaurant_repository = RestaurantRepository(db)
        self.sender = WhatsAppSender(db)

    def notify(self, *, order, restaurant_id: uuid.UUID) -> None:
        """Avisa o cliente da mudanca de status, se houver aviso para ela."""
        if not settings.WHATSAPP_NOTIFICATIONS_ENABLED:
            return

        kind = _KIND_POR_STATUS.get(order.status)
        if kind is None:
            return
        if kind == KIND_ENTREGUE and order.order_type not in ORDER_TYPES_QUE_SAO_ENTREGUES:
            return

        channel = self.channel_repository.resolve_for_branch(restaurant_id, order.branch_id)
        if channel is None:
            logger.info(
                "[WhatsApp] sem canal para avisar pedido=#%s filial=%s",
                order.order_number,
                order.branch_id,
            )
            return

        if self.message_repository.exists_for(order_id=order.id, kind=kind):
            logger.info(
                "[WhatsApp] aviso ja enviado, nada a fazer pedido=#%s aviso=%s",
                order.order_number,
                kind,
            )
            return

        self._send_and_record(order=order, restaurant_id=restaurant_id, channel=channel, kind=kind)

    def _send_and_record(
        self,
        *,
        order,
        restaurant_id: uuid.UUID,
        channel: WhatsAppChannel,
        kind: str,
    ) -> None:
        try:
            wamid = self.sender.send_template(
                channel=channel,
                to_phone=order.customer_phone_snapshot,
                template_name=_TEMPLATE_POR_KIND[kind],
                language=IDIOMA_DO_TEMPLATE,
                parameters=self._parameters(order, restaurant_id),
            )
        except WhatsAppSendRefused as recusa:
            logger.warning(
                "[WhatsApp] aviso nao enviado pedido=#%s aviso=%s motivo=%s",
                order.order_number,
                kind,
                recusa.reason,
            )
            self._record(order, channel, kind, status="failed", error_code=f"refused:{recusa.reason}")
            return
        except WhatsAppSendError as erro:
            logger.warning(
                "[WhatsApp] aviso recusado pela Meta pedido=#%s aviso=%s codigo=%s",
                order.order_number,
                kind,
                erro.error_code,
            )
            self._record(order, channel, kind, status="failed", error_code=erro.error_code)
            return

        logger.info(
            "[WhatsApp] aviso enviado pedido=#%s aviso=%s wamid=%s",
            order.order_number,
            kind,
            wamid,
        )
        self._record(order, channel, kind, status="sent", wamid=wamid)

    def _parameters(self, order, restaurant_id: uuid.UUID) -> tuple[str, str, str]:
        """Os tres `{{n}}` do template, na ordem em que ele os declara.

        A Meta nao nomeia parametro: trocar dois de lugar manda o numero do
        pedido onde vai o nome do cliente, sem erro nenhum.
        """
        restaurant = self.restaurant_repository.get_by_id(restaurant_id)
        return (
            _primeiro_nome(order.customer_name_snapshot),
            str(order.order_number),
            restaurant.name if restaurant is not None else "",
        )

    def _record(
        self,
        order,
        channel: WhatsAppChannel,
        kind: str,
        *,
        status: str,
        wamid: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self.db.add(
            WhatsAppMessage(
                order_id=order.id,
                channel_id=channel.id,
                kind=kind,
                status=status,
                wamid=wamid,
                error_code=error_code,
            )
        )
        self.db.commit()


def _primeiro_nome(nome: str) -> str:
    """"Maria Aparecida" vira "Maria".

    O template abre com "Ola, {{1}}!", e o nome inteiro do cadastro soa como
    correspondencia de banco. Nome vazio devolve vazio — a Meta aceita
    parametro vazio, e recusar o aviso inteiro por causa da saudacao seria
    trocar um cliente avisado por um cliente nao avisado.
    """
    partes = (nome or "").split()
    return partes[0] if partes else ""
