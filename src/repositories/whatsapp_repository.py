"""Consultas do canal de WhatsApp. Duas perguntas, e elas sao inversas.

"De qual numero esta filial fala?" e a do envio; "de quem e este numero?" e a
do webhook. As duas param aqui — o repositorio so consulta, quem decide e o
service.
"""

import uuid
from datetime import datetime

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.models.whatsapp_model import (
    WhatsAppChannel,
    WhatsAppContactWindow,
    WhatsAppMessage,
)



def _canal_utilizavel():
    """A condicao SQL de "da para mandar por este canal".

    Sao DUAS coisas, e elas nao se substituem:

        is_active        EU nao desliguei
        disconnected_at  a META nao tirou o acesso

    A funcao existe para que a resposta seja UMA. Repetir `is_active.is_(True)`
    em tres consultas foi o que quase aconteceu, e o dia em que a segunda
    condicao entrasse em duas delas e esquecesse a terceira seria um canal
    desconectado continuando a ser escolhido — sem erro nosso, com erro da
    Meta a cada aviso.
    """
    return and_(
        WhatsAppChannel.is_active.is_(True),
        WhatsAppChannel.disconnected_at.is_(None),
    )


def canal_utilizavel(canal: WhatsAppChannel) -> bool:
    """A MESMA regra em Python, e ela mora ao lado da de SQL de proposito.

    `resolve_for_branch` precisa da resposta sobre uma linha que ja esta em
    maos (a da filial), e nao de um `WHERE`. Duas formas da mesma pergunta em
    arquivos diferentes divergem no dia em que alguem mexe numa — e a
    armadilha 54 e exatamente esse caso, com a janela do cupom.
    """
    return canal.is_active and canal.disconnected_at is None


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
            _canal_utilizavel(),
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
            return da_filial if canal_utilizavel(da_filial) else None
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
                    # Recadastrar E reconectar: quem roda o script de
                    # novo esta com um token novo em maos, e o token
                    # novo so existe porque o lojista religou o acesso.
                    "disconnected_at": None,
                    "disconnect_reason": None,
                    "updated_at": func.now(),
                },
            )
            .returning(WhatsAppChannel)
        )
        return self.db.execute(stmt).scalar_one()

    def mark_disconnected_by_waba(
        self, *, waba_id: str, reason: str | None, now: datetime
    ) -> list[WhatsAppChannel]:
        """Marca TODOS os canais daquele WABA como desconectados.

        O evento e da CONTA e nao de um numero: quando o lojista tira o acesso
        da Cloud API, todos os numeros daquele WABA param junto. Marcar so um
        deixaria os outros tentando enviar contra um acesso que nao existe
        mais.

        Devolve o que mudou, para quem chama poder logar QUAIS numeros pararam
        — a linha de log e, hoje, o unico lugar onde isso aparece.

        Ja desconectado nao e reescrito: a Meta reenvia webhook, e sobrescrever
        moveria o `disconnected_at` para a frente a cada reenvio, apagando a
        hora em que a desconexao de fato aconteceu.
        """
        stmt = select(WhatsAppChannel).where(
            WhatsAppChannel.waba_id == waba_id,
            WhatsAppChannel.disconnected_at.is_(None),
        )
        canais = list(self.db.scalars(stmt))
        for canal in canais:
            canal.disconnected_at = now
            canal.disconnect_reason = reason
        return canais

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
            _canal_utilizavel(),
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

    def extend(
        self, *, channel_id: uuid.UUID, phone_e164: str, expires_at: datetime
    ) -> None:
        """Abre a janela, ou empurra a que ja existe — nunca a encurta.

        `GREATEST` e nao atribuicao, e e a diferenca inteira: a Meta REENVIA
        webhook e entrega fora de ordem. Um reenvio de mensagem antiga
        chegando depois da nova desfaria, com atribuicao, a janela que a nova
        abriu — e o proximo envio livre seria recusado por uma janela que
        estava aberta.
        """
        stmt = pg_insert(WhatsAppContactWindow).values(
            channel_id=channel_id,
            phone_e164=phone_e164,
            window_expires_at=expires_at,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_whatsapp_contact_windows_channel_phone",
            set_={
                "window_expires_at": func.greatest(
                    stmt.excluded.window_expires_at,
                    WhatsAppContactWindow.window_expires_at,
                ),
                "updated_at": func.now(),
            },
        )
        self.db.execute(stmt)


    def is_open(self, *, channel_id: uuid.UUID, phone_e164: str, now: datetime) -> bool:
        """Ha janela aberta para este telefone neste numero da loja?

        Ausencia de linha e janela FECHADA — que e o estado de quase todo
        cliente, porque ele pediu pelo app e nunca escreveu para a loja.
        """
        stmt = select(WhatsAppContactWindow.id).where(
            WhatsAppContactWindow.channel_id == channel_id,
            WhatsAppContactWindow.phone_e164 == phone_e164,
            WhatsAppContactWindow.window_expires_at > now,
        )
        return self.db.scalar(stmt) is not None


class WhatsAppMessageRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_wamid(self, wamid: str) -> WhatsAppMessage | None:
        return self.db.scalar(select(WhatsAppMessage).where(WhatsAppMessage.wamid == wamid))

    def exists_for(self, *, order_id: uuid.UUID, kind: str) -> bool:
        """Este aviso deste pedido ja saiu?

        Nao e redundante com `uq_whatsapp_messages_order_kind`: o UNIQUE barra
        a LINHA, e nesse ponto a mensagem ja foi entregue ao cliente. Ele
        protege a tabela; isto aqui protege o cliente de receber duas.
        """
        stmt = select(WhatsAppMessage.id).where(
            WhatsAppMessage.order_id == order_id,
            WhatsAppMessage.kind == kind,
        )
        return self.db.scalar(stmt) is not None

    def list_due_for_retry(self, *, now: datetime, limit: int) -> list[WhatsAppMessage]:
        """As linhas cuja retentativa ja venceu. A pergunta da varredura.

        **O filtro e `next_attempt_at`, e nao `status = 'failed'`.** Nem toda
        falha se conserta repetindo, e quem sabe disso e o TIPO da excecao, no
        instante em que ela aconteceu (armadilha 49). Perguntar pelo
        `error_code` aqui seria responder de novo, com outro vocabulario, uma
        pergunta que ja tem dono — e retentar um `132001` a cada dois minutos
        contra um template que so eu consigo aprovar.

        As mais antigas primeiro: elas sao as que estao mais perto de perder a
        validade, e a fila e drenada em lotes.

        **Sem `FOR UPDATE`, e a suposicao e que ha UMA varredura** — um
        container, um laco sequencial (`whatsapp-reenvio` no compose). E a
        mesma escolha das varreduras irmas de pedido, que reconferem o estado
        em vez de travar linha: o desfecho de cada uma e uma chamada externa,
        e o lock seria solto pelo primeiro `commit` do lote de qualquer jeito.

        O que uma segunda varredura simultanea custaria: o cliente recebendo o
        mesmo aviso duas vezes. Nao uma linha duplicada — disso o
        `UNIQUE (order_id, kind)` continua dando conta. Se um dia houver duas,
        e aqui que entra uma reserva de linha.
        """
        stmt = (
            select(WhatsAppMessage)
            .where(
                WhatsAppMessage.next_attempt_at.is_not(None),
                WhatsAppMessage.next_attempt_at <= now,
            )
            .order_by(WhatsAppMessage.created_at)
            .limit(limit)
        )
        return list(self.db.scalars(stmt))
