import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base
from src.models.product_option_model import ordem_dos_grupos


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False)
    # A FILIAL dona deste produto. Preco, disponibilidade e existencia sao
    # dela e de mais ninguem — nao ha heranca do restaurante e nao ha
    # sobrescrita (revisao 20260820_0026).
    #
    # As tres FKs compostas da revisao amarram esta coluna as outras:
    # (restaurant_id, branch_id) -> branches, (branch_id, category_id) ->
    # categories e (branch_id, printing_sector_id) -> printing_sectors. E o
    # que torna IMPOSSIVEL gravar produto de uma loja apontando para a
    # categoria ou a impressora de outra.
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"), nullable=False)
    # Setor de impressao (a praca que prepara este produto). NULO tem
    # significado proprio: o produto nao gera via de producao — e a lata de
    # refrigerante, que sai da geladeira do balcao. Ver a migracao
    # 20260809_0011 e src/services/admin_printing_service.py.
    printing_sector_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("printing_sectors.id"))
    code: Mapped[str | None] = mapped_column(Text)
    # A MESMA picanha nas duas lojas, para quem pergunta "quanto vendi de
    # picanha na rede".
    #
    # NAO tem semantica nenhuma de heranca: nada no cardapio, no pedido ou no
    # Rapi le esta coluna para decidir preco, disponibilidade ou qualquer
    # outra coisa. Ela existe so para RELATORIO — sem ela a pergunta acima
    # deixa de ter resposta por falta de chave, e e a unica coisa deste passo
    # que nao daria para acrescentar depois sem recadastrar tudo.
    #
    # Nao e o `code` acima: aquele e o codigo que o lojista imprime e busca no
    # painel, texto livre, sem unicidade, e entra no conteudo indexado do
    # Rapi. Duas semanticas na mesma coluna mudariam o significado de dado ja
    # gravado.
    #
    # Unico por (branch_id, catalog_key) quando preenchido: duas linhas da
    # MESMA loja com a mesma chave fariam o relatorio contar a mesma venda
    # duas vezes.
    catalog_key: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    # Para quantas pessoas o produto serve (revisao 20260825_0039).
    #
    # NASCEU PARA O ASSISTENTE DE VOZ E SOBREVIVEU A ELE (06/09/2026). O
    # backend nao le mais este campo em lugar nenhum: ele e preenchido pelo
    # lojista no painel e PUBLICADO em `ProductResponse`, no cardapio, para
    # quem quiser mostra-lo na tela. Nao foi derrubado junto com a voz porque
    # e dado do lojista — coluna apagada nao volta, e o que ele digitou sobre
    # 161 produtos nao se redigita.
    #
    # NULO quer dizer "o lojista nao disse", e nunca "serve para ninguem".
    # Nao ha default 1 de proposito: seria o banco inventando um fato sobre
    # todo produto ja cadastrado.
    serves_people: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    image_path: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    is_available: Mapped[bool | None] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int | None] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    restaurant = relationship("Restaurant", back_populates="products")
    branch = relationship("Branch")
    category = relationship("Category", back_populates="products")
    # `order_by` pela MESMA expressao que a query do painel usa. Sem ele o
    # `selectinload` emite um SELECT sem `ORDER BY` e o `sort_order` que o
    # lojista arrastou nao tem efeito nenhum na vitrine. Ver
    # `product_option_model.ordem_dos_grupos`.
    option_groups = relationship(
        "ProductOptionGroup",
        back_populates="product",
        order_by=ordem_dos_grupos,
    )
