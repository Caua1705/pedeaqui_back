"""Os avisos de status do pedido pelo WhatsApp.

Quatro: aceito, pronto para retirada, saiu para entrega, entregue. E eles
NAO valem todos para todo pedido — ver `_TIPOS_DE_PEDIDO_POR_AVISO`.

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

## E a falha que se conserta repetindo fica MARCADA, nao deduzida

`next_attempt_at` e preenchido no `except`, onde o TIPO da excecao existe —
`WhatsAppTransportError.retryable` e `True`, `WhatsAppRejectedError` e `False`
(armadilha 49). A varredura (`scripts/reenvia_avisos_de_whatsapp.py`) le a
coluna e nao olha o `error_code`: deduzir retentabilidade de um codigo da Meta
seria dar duas respostas a mesma pergunta, em dois arquivos.

O `132001` (template nao aprovado) fica de fora de proposito, e nao por
descuido: quem o conserta e uma aprovacao na Meta, que leva horas e e
operacao humana. Retenta-lo a cada dois minutos gastaria a validade inteira do
aviso sem nenhuma chance.

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
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from src.core.config import settings
from src.integrations.whatsapp_client import WhatsAppSendError
from src.models.whatsapp_model import WhatsAppChannel, WhatsAppMessage
from src.repositories.order_repository import OrderRepository
from src.repositories.restaurant_repository import RestaurantRepository
from src.repositories.whatsapp_repository import (
    WhatsAppChannelRepository,
    WhatsAppMessageRepository,
)
from src.services.whatsapp_send_service import WhatsAppSendRefused, WhatsAppSender


logger = logging.getLogger("uvicorn.error")

# Status do pedido -> aviso. So estes quatro avisam; o resto passa em
# silencio.
_KIND_POR_STATUS = {
    "accepted": "order_accepted",
    "ready": "order_ready_for_pickup",
    "out_for_delivery": "order_out_for_delivery",
    "completed": "order_delivered",
}

# Aviso -> nome do template aprovado na Meta.
_TEMPLATE_POR_KIND = {
    "order_accepted": "pedido_aceito",
    "order_ready_for_pickup": "pedido_pronto_para_retirada",
    "order_out_for_delivery": "pedido_saiu_para_entrega",
    "order_delivered": "pedido_entregue",
}

IDIOMA_DO_TEMPLATE = "pt_BR"

# CADA AVISO VALE NOS TIPOS DE PEDIDO EM QUE A FRASE DELE E VERDADEIRA.
#
# Nao e conservadorismo, sao quatro frases diferentes:
#
#   "foi aceito"            verdade nos dois
#   "pronto para retirada"  so na retirada. Na entrega, `ready` significa
#                           pronto para SAIR, e quem vem buscar e o
#                           motoboy — mandar isso e mandar a pessoa buscar
#                           um pedido que vai ate ela
#   "saiu para entrega"     so na entrega (o estado nem existe na retirada)
#   "foi entregue"          so na entrega. Na retirada, `completed` e "a
#                           pessoa veio buscar" — avisar seria contar a ela
#                           o que ela acabou de fazer
#
# LISTAS POSITIVAS, e escritas por extenso em vez de `ORDER_TYPES`
# (armadilha 47): tipo de pedido novo cai fora de TODAS e nasce sem aviso
# nenhum, que e o lado que fecha. Um aviso automatico com a frase errada e
# pior que aviso nenhum: o cliente age com base nele.
_TIPOS_DE_PEDIDO_POR_AVISO = {
    "order_accepted": ("delivery", "pickup"),
    "order_ready_for_pickup": ("pickup",),
    "order_out_for_delivery": ("delivery",),
    "order_delivered": ("delivery",),
}

# ATE QUANDO O AVISO AINDA VALE, contado da primeira tentativa.
#
# Ele nao e um dado, e um RECADO sobre um instante: "seu pedido foi aceito"
# chegando com o pedido ja entregue nao e um aviso atrasado, e um aviso
# errado — o cliente le e age. Passada a validade, a varredura DESISTE, e
# desistir e o desfecho certo, nao uma falha da varredura.
#
# 30 minutos porque e a ordem de grandeza do ciclo do pedido. O erro caro
# aqui e o numero GRANDE, ao contrario da carencia do
# `cancela_pedidos_sem_pagamento.py`, onde o erro caro e o curto.
VALIDADE_DO_AVISO = timedelta(minutes=30)

# Quanto esperar antes de tentar de novo. Constante, e nao exponencial: com
# 30 minutos de teto, um backoff gastaria a validade em tres tentativas: o
# que se quer aqui e atravessar uma queda de rede curta, e para isso a
# cadencia miuda e que serve. Quem limita e o relogio.
ESPERA_ENTRE_TENTATIVAS = timedelta(minutes=2)

# Estados em que a FRASE de qualquer aviso virou mentira, e reenviar e pior
# que desistir: "seu pedido foi aceito" para quem teve o pedido cancelado
# deixa o cliente esperando comida que nao vem.
#
# Escrito por extenso, e nao importado de `REVERSING_STATUSES`: aquela lista
# responde "que estados devolvem cupom e cashback", que e outra pergunta com
# a mesma resposta HOJE. Amarrar as duas faria um estorno novo mudar, de
# graca, o que o cliente recebe.
STATUS_QUE_MATAM_O_AVISO = ("cancelled", "rejected")

# Os desfechos de uma retentativa, para a varredura contar e imprimir.
REENVIO_ENVIADO = "enviado"
REENVIO_FALHOU = "falhou"
REENVIO_DESISTIU = "desistiu"


class WhatsAppOrderNotifier:
    def __init__(self, db: Session):
        self.db = db
        self.channel_repository = WhatsAppChannelRepository(db)
        self.message_repository = WhatsAppMessageRepository(db)
        self.order_repository = OrderRepository(db)
        self.restaurant_repository = RestaurantRepository(db)
        self.sender = WhatsAppSender(db)
        # Injetavel, pela convencao da armadilha 51: o teste do reenvio
        # declara o instante em vez de depender da hora em que roda.
        self.clock = lambda: datetime.now(timezone.utc)

    def notify(self, *, order, restaurant_id: uuid.UUID) -> None:
        """Avisa o cliente da mudanca de status, se houver aviso para ela."""
        if not settings.WHATSAPP_NOTIFICATIONS_ENABLED:
            return

        kind = _KIND_POR_STATUS.get(order.status)
        if kind is None:
            return
        if order.order_type not in _TIPOS_DE_PEDIDO_POR_AVISO[kind]:
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

    def retry(self, message: WhatsAppMessage) -> str:
        """Tenta de novo UM aviso que falhou. Devolve o desfecho.

        Chamada pela varredura (`scripts/reenvia_avisos_de_whatsapp.py`), uma
        linha por vez. Tudo que ela decide esta aqui, e nao la, porque as tres
        desistencias sao regra de dominio e nao de agendamento.

        As TRES perguntas antes de reenviar, e nenhuma e cerimonia — todas sao
        sobre o que mudou entre a falha e agora:

        1. **o aviso ainda vale?** Passada `VALIDADE_DO_AVISO`, a frase fala
           de um instante que ja passou;
        2. **o pedido ainda existe, e nao virou mentira?** Cancelado ou
           recusado, "seu pedido foi aceito" deixa o cliente esperando;
        3. **por qual numero se fala agora?** O canal e RESOLVIDO de novo, e
           nao lido do `channel_id` da linha: entre a falha e o reenvio o
           lojista pode ter desconectado a Cloud API (`disconnected_at`), e
           insistir no canal antigo e mandar contra um acesso que nao existe
           mais.
        """
        agora = self.clock()

        if message.created_at + VALIDADE_DO_AVISO <= agora:
            return self._desistir(message, motivo="validade vencida")

        order = self._order_of(message)
        if order is None:
            return self._desistir(message, motivo="pedido nao existe mais")
        if order.status in STATUS_QUE_MATAM_O_AVISO:
            return self._desistir(message, motivo=f"pedido em {order.status}")

        channel = self.channel_repository.resolve_for_branch(
            order.restaurant_id, order.branch_id
        )
        if channel is None:
            return self._desistir(message, motivo="sem canal utilizavel")

        enviado = self._send_and_record(
            order=order,
            restaurant_id=order.restaurant_id,
            channel=channel,
            kind=message.kind,
            existente=message,
            now=agora,
        )
        return REENVIO_ENVIADO if enviado else REENVIO_FALHOU

    def _order_of(self, message: WhatsAppMessage):
        """O pedido da linha, chegando nele pelo CANAL.

        A linha nao guarda `restaurant_id` — quem guarda e o canal por onde o
        aviso saiu. E `get_order_detail` exige o restaurante de proposito (um
        UUID de pedido em maos nao pode ler o pedido de outra loja), entao o
        caminho e esse.
        """
        channel = self.db.get(WhatsAppChannel, message.channel_id)
        if channel is None:
            return None
        return self.order_repository.get_order_detail(message.order_id, channel.restaurant_id)

    def _desistir(self, message: WhatsAppMessage, *, motivo: str) -> str:
        """Para de retentar, e deixa a linha dizendo que parou.

        `next_attempt_at = NULL` e o unico registro, e o `error_code` da falha
        original **nao e sobrescrito**: ele e a causa, e "desisti" e o
        desfecho — trocar um pelo outro apagaria a unica pista de por que o
        aviso nao saiu. Quem separa "desistiu" de "nunca foi retentavel" e o
        `attempts`, que passa de 1 num caso e nao no outro.
        """
        logger.warning(
            "[WhatsApp] desistindo do reenvio pedido_id=%s aviso=%s motivo=%s tentativas=%s",
            message.order_id,
            message.kind,
            motivo,
            message.attempts,
        )
        message.next_attempt_at = None
        self.db.commit()
        return REENVIO_DESISTIU

    def _send_and_record(
        self,
        *,
        order,
        restaurant_id: uuid.UUID,
        channel: WhatsAppChannel,
        kind: str,
        existente: WhatsAppMessage | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Manda e grava o desfecho. `True` se saiu.

        `existente` e a linha do reenvio: o `UNIQUE (order_id, kind)` diz que
        um aviso tem UMA linha, entao retentar ATUALIZA a que existe. Inserir
        outra seria o banco recusando o reenvio depois de a mensagem ja ter
        saido.
        """
        agora = now or self.clock()
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
            # Recusa NOSSA nunca e retentavel: o telefone do pedido nao muda
            # sozinho, e a janela fechada pede um template, nao uma repeticao.
            self._record(
                order,
                channel,
                kind,
                status="failed",
                error_code=f"refused:{recusa.reason}",
                existente=existente,
            )
            return False
        except WhatsAppSendError as erro:
            logger.warning(
                "[WhatsApp] aviso recusado pela Meta pedido=#%s aviso=%s codigo=%s retentavel=%s",
                order.order_number,
                kind,
                erro.error_code,
                erro.retryable,
            )
            self._record(
                order,
                channel,
                kind,
                status="failed",
                error_code=erro.error_code,
                next_attempt_at=agora + ESPERA_ENTRE_TENTATIVAS if erro.retryable else None,
                existente=existente,
            )
            return False

        logger.info(
            "[WhatsApp] aviso enviado pedido=#%s aviso=%s wamid=%s",
            order.order_number,
            kind,
            wamid,
        )
        self._record(order, channel, kind, status="sent", wamid=wamid, existente=existente)
        return True

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
        next_attempt_at: datetime | None = None,
        existente: WhatsAppMessage | None = None,
    ) -> None:
        if existente is None:
            self.db.add(
                WhatsAppMessage(
                    order_id=order.id,
                    channel_id=channel.id,
                    kind=kind,
                    status=status,
                    wamid=wamid,
                    error_code=error_code,
                    next_attempt_at=next_attempt_at,
                )
            )
            self.db.commit()
            return

        # O canal e reescrito porque o reenvio o resolveu de novo: se a loja
        # passou a falar por outro numero, o registro tem que dizer por qual
        # numero o aviso saiu de verdade.
        existente.channel_id = channel.id
        existente.status = status
        existente.wamid = wamid
        existente.error_code = error_code
        existente.next_attempt_at = next_attempt_at
        existente.attempts += 1
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
