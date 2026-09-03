"""Consultas do canal de WhatsApp. Duas perguntas, e elas sao inversas.

"De qual numero esta filial fala?" e a do envio; "de quem e este numero?" e a
do webhook. As duas param aqui — o repositorio so consulta, quem decide e o
service.
"""

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.models.whatsapp_model import WhatsAppChannel, WhatsAppContactWindow


class WhatsAppChannelRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_phone_number_id(self, phone_number_id: str) -> WhatsAppChannel | None:
        """A pergunta do webhook: de quem e este numero?

        So canal ATIVO. Um numero desligado do nosso lado e um numero que a
        aplicacao nao atende — e o webhook dele cai no mesmo caminho do
        numero desconhecido, que loga e responde 200.
        """
        stmt = select(WhatsAppChannel).where(
            WhatsAppChannel.phone_number_id == phone_number_id,
            WhatsAppChannel.is_active.is_(True),
        )
        return self.db.scalar(stmt)

    def resolve_for_branch(
        self, restaurant_id: uuid.UUID, branch_id: uuid.UUID
    ) -> WhatsAppChannel | None:
        """A pergunta do envio: de qual numero esta filial fala?

        Filial com numero usa o dela; filial sem numero herda o do
        restaurante. E o regime da armadilha 35, e a distincao e entre EXISTIR
        e nao existir linha — nunca entre verdadeiro e falso.

        **O numero da filial DESLIGADO nao cai no do restaurante.** Cair
        seria a loja passando a falar por outro numero sem ninguem ter
        pedido; o que se espera de um numero desligado e que a loja pare de
        mandar. Por isso a queda so acontece quando a filial nao tem linha
        nenhuma.
        """
        da_filial = self._get_by_branch(restaurant_id, branch_id)
        if da_filial is not None:
            return da_filial if da_filial.is_active else None
        return self._get_restaurant_default(restaurant_id)

    def upsert(
        self,
        *,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None,
        waba_id: str,
        phone_number_id: str,
        display_phone_number: str,
        access_token_encrypted: str,
    ) -> WhatsAppChannel:
        """Cadastra ou substitui o canal daquele `phone_number_id`.

        O conflito e pelo NUMERO e nao pela filial, porque o caso que se
        repete e a rotacao do token: o mesmo numero, com credencial nova. Um
        conflito por filial faria "trocar o numero da loja" e "renovar o
        token" serem a mesma operacao, e a primeira precisa apagar a linha
        antiga de proposito.
        """
        stmt = (
            pg_insert(WhatsAppChannel)
            .values(
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                waba_id=waba_id,
                phone_number_id=phone_number_id,
                display_phone_number=display_phone_number,
                access_token_encrypted=access_token_encrypted,
            )
            .on_conflict_do_update(
                constraint="uq_whatsapp_channels_phone_number_id",
                set_={
                    "restaurant_id": restaurant_id,
                    "branch_id": branch_id,
                    "waba_id": waba_id,
                    "display_phone_number": display_phone_number,
                    "access_token_encrypted": access_token_encrypted,
                    "is_active": True,
                    "updated_at": func.now(),
                },
            )
            .returning(WhatsAppChannel)
        )
        return self.db.execute(stmt).scalar_one()

    def _get_by_branch(
        self, restaurant_id: uuid.UUID, branch_id: uuid.UUID
    ) -> WhatsAppChannel | None:
        stmt = select(WhatsAppChannel).where(
            WhatsAppChannel.restaurant_id == restaurant_id,
            WhatsAppChannel.branch_id == branch_id,
        )
        return self.db.scalar(stmt)

    def _get_restaurant_default(self, restaurant_id: uuid.UUID) -> WhatsAppChannel | None:
        stmt = select(WhatsAppChannel).where(
            WhatsAppChannel.restaurant_id == restaurant_id,
            WhatsAppChannel.branch_id.is_(None),
            WhatsAppChannel.is_active.is_(True),
        )
        return self.db.scalar(stmt)


class WhatsAppContactWindowRepository:
    """A janela de 24h. Nesta rodada ela so nasce e morre.

    Quem a abre e o webhook (mensagem do cliente chegando); quem a le e o
    envio, para escolher entre texto livre e template.
    """

    def __init__(self, db: Session):
        self.db = db

    def delete_expired(self, now: datetime) -> int:
        """Apaga as janelas ja vencidas. E o expurgo do container `limpeza`.

        Aqui o prazo de retencao E a propria janela, e nao um numero de dias
        escolhido a parte: passada a hora, a linha nao responde mais nenhuma
        pergunta — e o que ela guarda e TELEFONE, numa tabela que nao pende
        de `customers` (armadilha 38).
        """
        stmt = delete(WhatsAppContactWindow).where(
            WhatsAppContactWindow.window_expires_at <= now
        )
        return self.db.execute(stmt).rowcount
