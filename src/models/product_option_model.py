import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, Boolean, ForeignKey, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


# A ORDEM DO CARDAPIO, definida uma vez so.
#
# Ate 02/09/2026 `option_groups` e `options` eram `relationship(...)` sem
# `order_by`, e `selectinload` NAO ordena: ele emite um segundo SELECT sem
# `ORDER BY` nenhum, e o Postgres devolve as linhas na ordem que quiser. O
# `sort_order` que o painel grava nao chegava na vitrine — e as duas telas
# podiam mostrar os mesmos adicionais em ordens diferentes, na mesma hora.
#
# Sao FUNCOES, e nao listas prontas, por dois motivos que se somam:
#
# - `relationship(order_by=...)` aceita callable, entao a query do painel e o
#   carregamento da vitrine usam LITERALMENTE a mesma expressao. Duas listas
#   parecidas divergiriam no dia em que alguem ajustasse uma delas — que e o
#   defeito que esta sendo consertado, so que uma camada acima;
# - chamadas na definicao da classe, elas rodariam antes de `ProductOption`
#   existir. O callable so e avaliado quando o mapper e configurado.
def ordem_dos_grupos() -> list:
    """Como os grupos de opcao aparecem, em qualquer tela."""
    return [ProductOptionGroup.sort_order, ProductOptionGroup.name, ProductOptionGroup.id]


def ordem_das_opcoes() -> list:
    """Como as opcoes aparecem dentro do grupo, em qualquer tela.

    O `id` no fim nao e decoracao: `sort_order` nasce 0 para todo mundo
    (`DEFAULT 0` na coluna), entao ate o lojista arrastar a lista o
    desempate e QUEM decide a ordem inteira. Sem uma chave unica no fim, duas
    linhas com o mesmo `sort_order` e o mesmo `name` podem sair trocadas entre
    duas requisicoes — o cardapio piscando sem nada ter mudado.
    """
    return [ProductOption.sort_order, ProductOption.name, ProductOption.id]


class ProductOptionGroup(Base):
    __tablename__ = "product_option_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    min_select: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_select: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int | None] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # NOT NULL no banco, com `DEFAULT now()` — e sem trigger de `updated_at`
    # nestas tabelas (conferido no `information_schema`), ao contrario de
    # `orders`. Entao quem move o `updated_at` e o `onupdate` daqui: sem ele a
    # coluna ficaria igual ao `created_at` para sempre, que e o que acontecia
    # enquanto o ORM nao a mapeava.
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    product = relationship("Product", back_populates="option_groups")
    options = relationship(
        "ProductOption",
        back_populates="option_group",
        order_by=ordem_das_opcoes,
    )


class ProductOption(Base):
    __tablename__ = "product_options"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    option_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("product_option_groups.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    additional_price: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=0)
    sort_order: Mapped[int | None] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # NOT NULL no banco, com `DEFAULT now()` — e sem trigger de `updated_at`
    # nestas tabelas (conferido no `information_schema`), ao contrario de
    # `orders`. Entao quem move o `updated_at` e o `onupdate` daqui: sem ele a
    # coluna ficaria igual ao `created_at` para sempre, que e o que acontecia
    # enquanto o ORM nao a mapeava.
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    option_group = relationship("ProductOptionGroup", back_populates="options")
