import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class MenuEvent(Base):
    """Um degrau do funil do cardapio: visita, produto aberto, item no
    carrinho, checkout iniciado.

    Ate esta tabela existir, a plataforma so enxergava PEDIDO. Poucos pedidos
    tinham dois diagnosticos opostos — ninguem entrou, ou quem entrou nao
    comprou — e nenhum jeito de distinguir os dois. Desenho completo em
    `docs/funil-e-origem.md`.

    ## O que ela NAO guarda, e por que cada ausencia e uma decisao

    **Nao guarda IP nem User-Agent.** Nenhum dos dois responde a qualquer um
    dos quatro degraus, e o IP e justamente o que transformaria a contagem
    num rastro de pessoa mesmo sem cookie. A base legal desta tabela e
    legitimo interesse, e ela so se sustenta com a minimizacao de fato feita.

    **Nao guarda `customer_id`.** Nem quando a pessoa esta logada.

    **E `orders` nao guarda `session_id`** — a outra metade da mesma decisao,
    e a mais importante das duas. Os quatro degraus se contam por
    `(filial, dia, origem)` e o quinto sai de `orders` pelo mesmo recorte:
    desenhar o funil NAO exige ligar sessao a pedido. Se exigisse, o rastro
    de navegacao inteiro passaria a estar amarrado a uma linha com nome,
    telefone e endereco, e esta tabela deixaria de ser contagem anonima para
    virar historico comportamental identificado.

    O preco disso e conhecido e aceito: nao da para responder "esta pessoa viu
    seis produtos antes de comprar".

    ## A retencao NAO e higiene de disco

    E o mecanismo de exclusao desta tabela. Sem `customer_id`, a exclusao de
    conta (`customer_anonymization_service`) nao alcanca linha nenhuma daqui
    — exatamente a situacao de `ai_feedback` e do comentario de
    `order_reviews`, e a resposta ja escolhida nas duas foi a mesma: prazo
    curto. Quem sabe qual e o prazo, e por que, e
    `menu_event_service.menu_event_retention_cutoff`.
    """

    __tablename__ = "menu_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('menu_view', 'product_view', 'cart_add', 'checkout_start')",
            name="ck_menu_events_event_type",
        ),
        # O relatorio: sempre uma filial (ou a lista delas) dentro de um
        # periodo.
        Index("ix_menu_events_branch_occurred_at", "branch_id", "occurred_at"),
        # O expurgo, que varre por data SEM filial e por isso nao aproveita o
        # indice acima — a coluna lider nao bate.
        #
        # BRIN e nao btree: a tabela e append-only e ordenada no tempo, que e
        # o caso de livro do BRIN. Ele fica na casa dos kilobytes onde um
        # btree teria centenas de megabytes, e o tamanho aqui nao e vaidade:
        # esta tabela escreve MUITO mais do que le, e todo indice e custo em
        # cada INSERT do caminho quente do cardapio.
        Index(
            "ix_menu_events_occurred_at_brin",
            "occurred_at",
            postgresql_using="brin",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False
    )
    # NOT NULL, sem queda para filial padrao: o cardapio e da filial
    # (armadilha 36) e evento sem loja nao responde nada. "As duas somadas" e
    # um GROUP BY, nao um nulo.
    #
    # `restaurant_id` ao lado nao e redundancia solta: a FK composta
    # (restaurant_id, branch_id) -> branches(restaurant_id, id) impede que os
    # dois divirjam, e e ela que dispensa a rota de conferir a filial com um
    # SELECT a cada lote. Ela vive so no banco, como as quatro irmas da
    # revisao 20260820_0026.
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    # Identificador opaco gerado pelo CLIENTE, so para agrupar contagem
    # ("quantas sessoes chegaram ao degrau 3"). Entrada nao confiavel: nunca
    # e chave de acesso a coisa nenhuma, e o tamanho e limitado no schema.
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Nunca NULL — ver DEFAULT_TRAFFIC_SOURCE em core/constants.py.
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # So em `product_view` e `cart_add`. FK e segura porque nada some do
    # cardapio, so desativa.
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id")
    )
    # DO SERVIDOR, nunca do cliente. Relogio de celular erra, as vezes por
    # meses, e um evento datado em 2027 nao aparece em erro nenhum: ele
    # simplesmente some da janela do relatorio, e o numero fica errado para
    # sempre sem nada no log.
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
