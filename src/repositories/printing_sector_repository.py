"""Consultas dos setores de impressao.

`printing_sectors` nao tem `restaurant_id`: o setor pertence a uma FILIAL
(a impressora esta fisicamente nela). Entao toda consulta que precisa
confrontar o token do lojista chega ao restaurante pela juncao com
`branches` — o mesmo arranjo que AdminMenuRepository usa para alcancar
grupo e opcao a partir de `products`. E o que impede um id de setor de
outro restaurante de ser editado por quem descobriu o UUID.
"""

import uuid

from sqlalchemy import Row, select, update
from sqlalchemy.orm import Session

from src.models.branch_model import Branch
from src.models.printing_sector_model import PrintingSector
from src.models.product_model import Product


class PrintingSectorRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_by_branch(
        self,
        branch_id: uuid.UUID,
        include_inactive: bool = True,
    ) -> list[PrintingSector]:
        """Setores da filial.

        Traz os desativados por padrao pelo mesmo motivo do cardapio do
        painel: quem desligou um setor precisa continuar vendo-o para
        religar.
        """
        conditions = [PrintingSector.branch_id == branch_id]
        if not include_inactive:
            conditions.append(PrintingSector.is_active.is_(True))
        stmt = (
            select(PrintingSector)
            .where(*conditions)
            .order_by(PrintingSector.sort_order.asc(), PrintingSector.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get(self, sector_id: uuid.UUID, restaurant_id: uuid.UUID) -> PrintingSector | None:
        # A juncao com branches e o que confere o restaurante: o setor em si
        # so conhece a filial dele.
        stmt = (
            select(PrintingSector)
            .join(Branch, Branch.id == PrintingSector.branch_id)
            .where(
                PrintingSector.id == sector_id,
                Branch.restaurant_id == restaurant_id,
            )
        )
        return self.db.scalar(stmt)

    def get_by_name(
        self,
        name: str,
        branch_id: uuid.UUID,
    ) -> PrintingSector | None:
        stmt = select(PrintingSector).where(
            PrintingSector.branch_id == branch_id,
            PrintingSector.name == name,
        )
        return self.db.scalar(stmt)

    def add(self, sector: PrintingSector) -> PrintingSector:
        self.db.add(sector)
        self.db.flush()
        return sector

    def list_sectors_of_products(
        self,
        product_ids: list[uuid.UUID],
        restaurant_id: uuid.UUID,
    ) -> list[Row]:
        """Par (produto, setor) para os produtos de um pedido.

        LEFT JOIN e nao INNER: o produto SEM setor precisa aparecer aqui
        para o service saber a diferenca entre "nao imprime na producao"
        (coluna nula) e "produto que nem esta mais neste restaurante".

        Sem filtro por filial nem por `is_active` do setor de proposito. Um
        setor de outra filial ou ja desativado nao serve para imprimir, mas
        quem decide o que fazer com esse item e o service — e ele manda o
        item para a via "SEM SETOR" em vez de deixa-lo sumir da comanda.
        """
        if not product_ids:
            return []
        stmt = (
            select(Product.id, PrintingSector)
            .outerjoin(PrintingSector, PrintingSector.id == Product.printing_sector_id)
            .where(
                Product.id.in_(product_ids),
                Product.restaurant_id == restaurant_id,
            )
        )
        return list(self.db.execute(stmt).all())

    def assign_sector_to_category(
        self,
        category_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        sector_id: uuid.UUID | None,
    ) -> int:
        """Aponta todos os produtos da categoria para o mesmo setor.

        UPDATE em massa e nao um laco em Python porque a operacao existe
        justamente para a categoria grande ("as 80 bebidas vao para o Bar"),
        e carregar 80 objetos so para gravar uma coluna e trabalho a toa.

        `restaurant_id` entra no WHERE junto com a categoria: sem ele, um
        UUID de categoria alheia reescreveria o cardapio de outro
        restaurante.
        """
        stmt = (
            update(Product)
            .where(
                Product.category_id == category_id,
                Product.restaurant_id == restaurant_id,
            )
            .values(printing_sector_id=sector_id)
        )
        return self.db.execute(stmt).rowcount or 0
