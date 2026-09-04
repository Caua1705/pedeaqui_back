"""O webhook do WhatsApp: confere a assinatura, roteia pelo numero, aplica.

## A ordem, e por que ela e o CONTRARIO da do pagamento

`PaymentService.handle_webhook` acha o pedido ANTES de conferir a assinatura,
e nao por escolha: o segredo do Mercado Pago e do RESTAURANTE, entao nao ha
como saber qual usar sem antes descobrir de quem e o pagamento.

Aqui o segredo e o App Secret do NOSSO app da Meta, um so para a aplicacao
inteira. Da para conferir a primeira coisa, e e o que se faz: **nada acontece
antes da assinatura** — nem leitura do corpo, nem consulta ao banco.

## `phone_number_id` desconhecido: 200, com log, e nada mais

Tres motivos, e o terceiro e o que aparece na operacao:

- **reenviar nao conserta.** A Meta retenta em cima de qualquer resposta que
  nao seja 2xx e desativa o webhook do app depois de falha sustentada. E o
  webhook e o mesmo para TODOS os restaurantes: devolver 5xx por um numero
  que nao e nosso troca um evento inutil por um canal derrubado;
- **nao ha o que proteger.** Sem canal nao ha filial, nao ha pedido e nao ha
  estado a mudar;
- **um numero novo do proprio lojista cai aqui.** E o sintoma de "conectei o
  numero na Meta e esqueci de cadastrar do nosso lado", e por isso o log leva
  o `display_phone_number` que vem no proprio payload — ele e publico (e o
  numero comercial da loja) e e o que permite cadastrar sem abrir o painel da
  Meta.

## O status so AVANCA

A Meta reenvia e entrega fora de ordem. Um `sent` chegando depois de um
`read` faria a linha mentir sobre o que aconteceu, entao a escrita e
condicionada a ordem — a mesma ideia do `GREATEST` da janela.

E status que existe do lado DELES e nao do nosso (`deleted`, `warning`) e
ignorado, nunca gravado: e a armadilha 15 vista de fora — valor so no lado
deles morre no INSERT contra o CHECK, e o POST inteiro cairia por causa de um
status que nao decide nada.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status as http_status
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.constants import WHATSAPP_CUSTOMER_WINDOW_HOURS, WHATSAPP_MESSAGE_STATUSES
from src.integrations.whatsapp_client import (
    WhatsAppAccountUpdate,
    WhatsAppChange,
    WhatsAppNotConfiguredError,
    WhatsAppStatusUpdate,
    WhatsAppWebhookPayloadError,
    parse_account_updates,
    parse_webhook_changes,
    verify_webhook_signature,
)
from src.models.whatsapp_model import WhatsAppChannel
from src.repositories.whatsapp_repository import (
    WhatsAppChannelRepository,
    WhatsAppContactWindowRepository,
    WhatsAppMessageRepository,
)


logger = logging.getLogger("uvicorn.error")

# Ordem de vida de uma mensagem. `failed` no topo porque e terminal: uma
# mensagem que falhou nao passa a ser entregue depois, e um `sent` atrasado
# chegando em cima dela nao pode apagar a falha.
_ORDEM_DO_STATUS = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}

# O unico evento de conta em que agimos. Os outros sao LOGADOS e nada mais:
# agir sobre um evento que ninguem estudou e inventar comportamento, e o
# log e o que vai dizer se algum dia um deles importa.
EVENTO_DE_DESCONEXAO = "PARTNER_REMOVED"


class WhatsAppWebhookService:
    def __init__(self, db: Session):
        self.db = db
        self.channel_repository = WhatsAppChannelRepository(db)
        self.window_repository = WhatsAppContactWindowRepository(db)
        self.message_repository = WhatsAppMessageRepository(db)

    def handle(self, *, raw_body: bytes, headers: dict[str, str]) -> dict[str, Any]:
        self._verify_signature(raw_body, headers)

        try:
            changes = parse_webhook_changes(raw_body)
        except WhatsAppWebhookPayloadError as exc:
            # 200 de proposito: reenviar nao conserta um corpo que nao
            # entendemos, e 5xx poria a Meta em retentativa por horas.
            logger.warning("[WhatsApp] webhook ignorado motivo=%s", exc)
            return {"status": "ignored", "reason": "payload"}

        # Os eventos de CONTA sao lidos do MESMO corpo, por outra funcao: a
        # chave deles e o WABA, e nao o `phone_number_id` (ver
        # `parse_account_updates`). Sem esta segunda leitura, o aviso de
        # desconexao seria descartado em silencio pelo parser de numero — e
        # silencio e o que ele existe para acabar.
        eventos = parse_account_updates(raw_body)

        if not changes and not eventos:
            return {"status": "ignored", "reason": "empty"}

        roteadas = self._apply_account_updates(eventos)
        for change in changes:
            channel = self.channel_repository.get_by_phone_number_id(change.phone_number_id)
            if channel is None:
                logger.warning(
                    "[WhatsApp] numero desconhecido phone_number_id=%s numero=%s",
                    change.phone_number_id,
                    change.display_phone_number,
                )
                continue
            self._apply(change, channel)
            roteadas += 1

        if roteadas == 0:
            return {"status": "ignored", "reason": "unknown_number"}

        self.db.commit()
        return {"status": "ok"}

    def _verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> None:
        try:
            valida = verify_webhook_signature(
                raw_body=raw_body,
                headers=headers,
                app_secret=settings.WHATSAPP_APP_SECRET,
            )
        except WhatsAppNotConfiguredError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc

        if not valida:
            # 401 e nao 200: assinatura invalida e a unica hipotese em que
            # alguem esta tentando falar pelo numero do lojista.
            logger.warning("[WhatsApp] webhook com assinatura invalida")
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="Assinatura do webhook inválida",
            )

    def _apply_account_updates(self, eventos: list[WhatsAppAccountUpdate]) -> int:
        """Desliga os canais do WABA que o lojista desconectou.

        Devolve quantos eventos foram TRATADOS — inclusive os que nao
        derrubaram canal nenhum. Um `PARTNER_REMOVED` de um WABA que nao e
        nosso ainda e um webhook atendido, e responder `unknown_number` a ele
        seria mentir sobre o que aconteceu.
        """
        tratados = 0
        for evento in eventos:
            tratados += 1
            if evento.event != EVENTO_DE_DESCONEXAO:
                # Nao agimos, mas o log fica: e ele que vai dizer se algum dia
                # outro evento importa.
                logger.info(
                    "[WhatsApp] evento de conta sem acao evento=%s waba=%s",
                    evento.event,
                    evento.waba_id,
                )
                continue
            self._disconnect(evento)
        return tratados

    def _disconnect(self, evento: WhatsAppAccountUpdate) -> None:
        canais = self.channel_repository.mark_disconnected_by_waba(
            waba_id=evento.waba_id,
            reason=evento.reason,
            now=datetime.now(timezone.utc),
        )
        if not canais:
            logger.warning(
                "[WhatsApp] desconexao de um WABA sem canal nosso waba=%s motivo=%s",
                evento.waba_id,
                evento.reason,
            )
            return

        # WARNING e nao INFO: daqui em diante NENHUM aviso sai por estes
        # numeros, e a unica forma de religar e o lojista reconectar. E a
        # linha que faz a diferenca entre descobrir hoje e descobrir pela
        # reclamacao do cliente.
        logger.warning(
            "[WhatsApp] canal DESCONECTADO pelo lojista waba=%s motivo=%s numeros=%s",
            evento.waba_id,
            evento.reason,
            ", ".join(canal.display_phone_number for canal in canais),
        )

    def _apply(self, change: WhatsAppChange, channel: WhatsAppChannel) -> None:
        for mensagem in change.inbound:
            self.window_repository.extend(
                channel_id=channel.id,
                phone_e164=mensagem.from_phone,
                # O relogio e o da MENSAGEM, e nao o nosso: um webhook que
                # chega meia hora atrasado nao pode esticar a janela em meia
                # hora.
                expires_at=mensagem.sent_at + timedelta(hours=WHATSAPP_CUSTOMER_WINDOW_HOURS),
            )
        for atualizacao in change.statuses:
            self._apply_status(atualizacao)

    def _apply_status(self, atualizacao: WhatsAppStatusUpdate) -> None:
        if atualizacao.status not in WHATSAPP_MESSAGE_STATUSES:
            logger.info(
                "[WhatsApp] status fora do nosso conjunto, ignorado status=%s wamid=%s",
                atualizacao.status,
                atualizacao.wamid,
            )
            return

        mensagem = self.message_repository.get_by_wamid(atualizacao.wamid)
        if mensagem is None:
            # Mensagem que nao saiu daqui. Nao e erro: o mesmo numero pode
            # ser usado por uma pessoa no painel da Meta.
            return

        if _ORDEM_DO_STATUS[atualizacao.status] <= _ORDEM_DO_STATUS[mensagem.status]:
            return

        mensagem.status = atualizacao.status
        if atualizacao.error_code is not None:
            mensagem.error_code = atualizacao.error_code
