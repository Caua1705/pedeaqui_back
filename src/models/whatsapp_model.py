"""As tres tabelas do WhatsApp: o numero, a janela de 24h e o que foi enviado.

Ficam no mesmo arquivo porque so se leem juntas — a janela e por canal e a
mensagem sai por um canal. Separadas, o motivo de cada uma existir teria que
ser repetido em tres cabecalhos.

## Por que o canal e TABELA, e nao coluna em `branches`

A semantica pedida e a da armadilha 35 — filial com numero usa o dela, filial
sem numero herda o do restaurante, `NULL` significa "herda" — e essa parte
daria certo com uma coluna em `branches` e outra em `restaurant_settings`.

O que nao daria e a pergunta INVERSA, que e a do webhook: ele chega com um
`phone_number_id` e precisa achar a filial. Espalhado em duas tabelas isso
vira dois SELECT e, pior, **nao existe UNIQUE que atravesse duas tabelas** —
o mesmo numero cadastrado nos dois lugares rotearia para dois destinos, sem
erro, ate o dia em que os dois divergissem. Numa tabela so, o
`UNIQUE (phone_number_id)` e o banco garantindo que a resposta e uma.

## O token e do LOJISTA, e mora cifrado

Mesmo desenho de `RestaurantPaymentCredential.access_token_encrypted`: Fernet
(`src/utils/crypto.py`), chave no `.env`, decifrado na hora do envio e nunca
guardado em atributo de objeto de vida longa nem em log. A chave e PROPRIA
(`WHATSAPP_TOKEN_ENCRYPTION_KEY`), sem queda para a do pagamento — armadilha
32.

O que NAO mora aqui e o App Secret da Meta: ele e nosso, e um so para a
aplicacao inteira, e assina o webhook de todos os numeros. Ver
`settings.WHATSAPP_APP_SECRET`.

## O que muda quando o onboarding virar automatico

Nada nesta tabela. Hoje a linha e escrita a mao por
`scripts/register_whatsapp_channel.py`; com Tech Provider ela passa a ser
escrita pelo retorno do Embedded Signup. Muda de onde o token CHEGA, nao onde
ele fica.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    text,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class WhatsAppChannel(Base):
    """Um numero conectado a Cloud API.

    `branch_id` nulo e a linha do RESTAURANTE: a queda de quem nao tem numero
    proprio. Restaurante de uma filial so deixa o campo vazio e funciona como
    "um numero por restaurante" sem nenhum caso especial no codigo.
    """

    __tablename__ = "whatsapp_channels"
    __table_args__ = (
        # A chave de roteamento do webhook. Um numero aponta para UM destino.
        UniqueConstraint("phone_number_id", name="uq_whatsapp_channels_phone_number_id"),
        # Uma filial tem no maximo um numero. Nao cobre a linha de
        # restaurante: `NULL` e distinto de `NULL` no Postgres, e por isso o
        # indice parcial da migracao existe.
        UniqueConstraint("branch_id", name="uq_whatsapp_channels_branch_id"),
        Index(
            "ux_whatsapp_channels_restaurant_default",
            "restaurant_id",
            unique=True,
            postgresql_where=text("branch_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False
    )
    # NULO = o numero do restaurante, usado por toda filial que nao tem o
    # seu. E o regime da armadilha 35, e vale a mesma regra: `NULL` significa
    # "herda", e so `NULL`.
    #
    # No banco esta coluna entra numa FK COMPOSTA
    # `(restaurant_id, branch_id) -> branches (restaurant_id, id)`, o mesmo
    # cinto do cardapio por filial (armadilha 36): sem ela daria para pendurar
    # a filial de um restaurante debaixo de outro. Com `branch_id` nulo a FK
    # composta nao e conferida (`MATCH SIMPLE`), que e exatamente o que a
    # linha de restaurante precisa.
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    waba_id: Mapped[str] = mapped_column(Text, nullable=False)
    # O `phone_number_id` da Meta. E ele que vem no `metadata` de todo webhook
    # e e por ele que se acha a filial — nao pelo numero legivel.
    phone_number_id: Mapped[str] = mapped_column(Text, nullable=False)
    # O numero como uma pessoa o le ("+55 85 99999-0000"). Nao serve para
    # rotear nada: existe para a tela, para o log do numero desconhecido e
    # para eu conferir que cadastrei o par certo.
    display_phone_number: Mapped[str] = mapped_column(Text, nullable=False)
    # Cifrado com Fernet, chave em settings.WHATSAPP_TOKEN_ENCRYPTION_KEY.
    # Nunca decodificar isto em log nem em resposta de API.
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Desligar o numero de uma filial faz a loja PARAR de mandar — nao faz
    # ela herdar o do restaurante. Herdar seria a loja passando a falar por
    # outro numero sem ninguem ter pedido.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # --- Desconectado PELA META, que nao e a mesma coisa que desligado por nos.
    #
    #   is_active = False   EU desliguei este numero
    #   disconnected_at     o LOJISTA tirou o acesso da Cloud API pelo
    #                       aplicativo dele, e a Meta avisou com
    #                       `account_update` / `PARTNER_REMOVED`
    #
    # As duas saidas sao opostas: a primeira se desfaz no nosso painel, a
    # segunda so se desfaz com o lojista reconectando. Por isso sao duas
    # colunas — e por isso as duas sao conferidas JUNTAS, em
    # `whatsapp_repository._canal_utilizavel()`.
    #
    # `disconnect_reason` e nulavel porque `disconnection_info` e CONDICIONAL
    # na Meta: nulo aqui e "desconectou e nao disseram por que", um estado
    # legitimo e nao um dado faltando.
    disconnected_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    disconnect_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WhatsAppContactWindow(Base):
    """A janela de 24h de um telefone naquele numero da loja.

    A Meta so aceita texto livre enquanto o cliente tiver escrito para aquele
    numero nas ultimas 24 horas. Fora dela, so template aprovado. Esta tabela
    e a unica fonte dessa resposta: sem ela, "dentro da janela" seria um
    palpite, e o palpite errado e uma mensagem que a Meta recusa e um cliente
    que nao e avisado.

    **Ela guarda telefone**, que e dado pessoal numa tabela que nao pende de
    `customers`. Armadilha 38: nasce com prazo, e o prazo E o mecanismo de
    exclusao — a linha vencida nao serve para mais nada e e apagada por
    `scripts/cleanup_idempotency_keys.py`.

    O que ela NAO guarda e o TEXTO da mensagem que chegou. Guardar seria dado
    pessoal a mais, com prazo a mais, para uma leitura que nao existe nesta
    rodada.
    """

    __tablename__ = "whatsapp_contact_windows"
    __table_args__ = (
        UniqueConstraint(
            "channel_id", "phone_e164", name="uq_whatsapp_contact_windows_channel_phone"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Digitos com DDI, sem `+` — a forma que a Meta usa no campo `from` do
    # webhook e no `to` do envio.
    phone_e164: Mapped[str] = mapped_column(Text, nullable=False)
    window_expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WhatsAppMessage(Base):
    """Um aviso que saiu (ou tentou sair) para o cliente de um pedido.

    Sem esta tabela, "o cliente foi avisado" e suposicao: a Meta aceita a
    chamada e entrega depois, e o desfecho chega em outro webhook. E o
    `status` daqui que separa "mandei" de "chegou".

    `UNIQUE (order_id, kind)` fecha o aviso repetido — clique duplo, retry de
    rede, varredura futura — de graca e no banco, que e onde ele fica valendo
    para todas as portas de uma vez.

    Sem telefone nenhum aqui: quem tem o telefone e o pedido.
    """

    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        UniqueConstraint("order_id", "kind", name="uq_whatsapp_messages_order_kind"),
        # O `wamid` e a chave pela qual o webhook de status acha esta linha.
        # Nulo quando a chamada falhou antes de a Meta responder: `NULL` e
        # distinto de `NULL`, entao varias falhas convivem.
        UniqueConstraint("wamid", name="uq_whatsapp_messages_wamid"),
        CheckConstraint(
            "kind = ANY (ARRAY['order_accepted'::text, "
            "'order_ready_for_pickup'::text, 'order_out_for_delivery'::text, "
            "'order_delivered'::text])",
            name="ck_whatsapp_messages_kind",
        ),
        CheckConstraint(
            "status = ANY (ARRAY['sent'::text, 'delivered'::text, 'read'::text, "
            "'failed'::text])",
            name="ck_whatsapp_messages_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    # Sem `ON DELETE`: apagar um canal apagaria o registro de que o cliente
    # foi avisado. Numero que sai de operacao e DESATIVADO (`is_active`), a
    # mesma regra do cardapio.
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("whatsapp_channels.id"), nullable=False
    )
    # Qual aviso, no nosso vocabulario. NAO e o nome do template na Meta —
    # ver WHATSAPP_MESSAGE_KINDS.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # O id da mensagem na Meta. Nulo quando nem chegou a existir uma.
    wamid: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Por que o aviso nao saiu. DOIS vocabularios, e o prefixo separa os dois:
    #
    #   `131047`, `132001`   codigo da META — ela respondeu e recusou
    #   `refused:phone`      NOSSA recusa — o telefone do pedido nao vira
    #   `refused:window`     E.164 sem chute, ou seria texto livre fora da
    #                        janela de 24h
    #
    # Sem o prefixo, um `132001` e um "telefone torto" pareceriam o mesmo
    # tipo de problema — e sao: um pede mexer na Meta, o outro no cadastro do
    # cliente. TEXTO e nao numero porque e o que se cita num chamado.
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
