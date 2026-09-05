"""Quantos templates de WhatsApp sairam, por restaurante e por periodo. Nao commita.

Le so `whatsapp_messages`, e nao precisou de coluna nova nenhuma: `channel_id`
diz de quem e o numero, `kind` diz qual template, `wamid` diz se a mensagem
chegou a existir na Meta e `created_at` diz quando. Ver a revisao
`20260905_0053` e o model.

## Por que o eixo e o CANAL, e nao o pedido

`whatsapp_messages` tem `order_id` tambem, e o pedido tambem sabe de qual
restaurante ele e. Os dois responderiam igual hoje, e o canal e o certo pelo
motivo que importa aqui: **a Meta cobra o numero**, e o numero e o canal. No
dia em que uma filial mandar aviso de pedido pelo numero de outra — que a
tabela permite, porque `channel_id` e coluna propria e nao derivada —, e o
canal que diz de qual conta saiu a cobranca.
"""

import uuid
from datetime import datetime

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from src.models.restaurant_model import Restaurant
from src.models.whatsapp_model import WhatsAppChannel, WhatsAppMessage


#: O prefixo que separa os dois vocabularios de `error_code` (ver o model): com
#: ele, a recusa foi NOSSA e a Meta nunca soube da mensagem; sem ele, o codigo e
#: dela e a chamada chegou a sair daqui.
PREFIXO_DA_RECUSA_LOCAL = "refused:"


class WhatsAppUsageRepository:
    def __init__(self, db: Session):
        self.db = db

    def templates_por_restaurante_e_tipo(
        self,
        desde: datetime,
        ate: datetime,
        restaurant_id: uuid.UUID | None = None,
    ) -> list[Row]:
        """Uma linha por (restaurante, tipo de aviso) na janela `[desde, ate)`.

        Por tipo e nao so por restaurante porque os quatro avisos nao sao
        intercambiaveis: `order_accepted` sai para todo pedido aceito e os
        outros tres dependem de o pedido chegar aonde eles descrevem. Um total
        que dobrou sem que a contagem por tipo diga qual dobrou nao responde
        nada.

        **`enviados` conta `wamid IS NOT NULL`, e nao `status`.** O `wamid` e o
        id que a Meta devolve: ele existe se e so se a mensagem existiu la. O
        `status` continua andando depois disso (`sent` -> `delivered` -> `read`,
        ou `failed` numa entrega que nao completou), e uma mensagem que a Meta
        aceitou e nao entregou **ja saiu** — contar por `status` faria a conta
        mudar depois, sozinha, por causa de um webhook.

        `recusados_aqui` sao os que nunca tocaram a Meta: telefone que nao vira
        E.164, texto livre fora da janela de 24h. Eles nao custam nada e nao
        podem entrar no mesmo balde de uma recusa dela.
        """
        saiu = WhatsAppMessage.wamid.is_not(None)
        recusa_nossa = WhatsAppMessage.error_code.startswith(PREFIXO_DA_RECUSA_LOCAL)

        stmt = (
            select(
                Restaurant.id.label("restaurant_id"),
                Restaurant.name.label("restaurante"),
                WhatsAppMessage.kind.label("tipo"),
                func.count().label("templates"),
                func.count().filter(saiu).label("enviados"),
                func.count().filter(recusa_nossa).label("recusados_aqui"),
            )
            .join(WhatsAppChannel, WhatsAppChannel.id == WhatsAppMessage.channel_id)
            .join(Restaurant, Restaurant.id == WhatsAppChannel.restaurant_id)
            .where(
                WhatsAppMessage.created_at >= desde,
                WhatsAppMessage.created_at < ate,
            )
            .group_by(Restaurant.id, Restaurant.name, WhatsAppMessage.kind)
            .order_by(Restaurant.name, WhatsAppMessage.kind)
        )
        if restaurant_id is not None:
            stmt = stmt.where(WhatsAppChannel.restaurant_id == restaurant_id)

        return list(self.db.execute(stmt).all())
