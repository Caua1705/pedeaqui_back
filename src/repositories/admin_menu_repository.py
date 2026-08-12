"""Consultas do cardapio no painel (BLOCO B da Fase 3).

Separado de `MenuRepository` e `ProductRepository`, que servem o cardapio
publico e filtram `is_active` / `is_available` em toda consulta. Aqui e o
oposto: o lojista precisa enxergar justamente o que esta desligado, senao
nao tem como religar.

Toda consulta recebe `restaurant_id`. Grupo de opcao e opcao nao tem essa
coluna, entao chegam nele pela juncao com `products` — e o que impede um id
de outro restaurante de ser editado por quem descobriu o UUID.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from src.models.category_model import Category
from src.models.product_model import Product
from src.models.product_option_model import ProductOption, ProductOptionGroup
from src.utils.normalization import normalize_text


def _build_product_search_condition(search: str):
    """Busca de produto no painel: nome ou codigo.

    `%` e `_` sao escapados pelo mesmo motivo da busca de pedidos: sem isso
    um "%" digitado sozinho lista tudo e um produto chamado "Combo_1" nunca
    e encontrado.

    O `normalize_text` poe o termo em NFC porque o ILIKE compara BYTES: o
    "é" composto e o decomposto sao a mesma letra na tela e strings
    diferentes no banco. Os nomes gravados passam pelo mesmo NFC no schema
    de escrita — os dois lados juntos e que fazem a busca encontrar.
    """
    padrao = f"%{_escape_like(normalize_text(search))}%"
    return Product.name.ilike(padrao, escape="\\") | Product.code.ilike(padrao, escape="\\")


def _escape_like(termo: str) -> str:
    """Neutraliza os curingas do LIKE dentro do que o lojista digitou.

    A contrabarra vem primeiro: escapando-a depois, ela escaparia tambem as
    contrabarras que as duas linhas seguintes acabaram de inserir.
    """
    return termo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class AdminMenuRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_categories(self, restaurant_id: uuid.UUID) -> list[Category]:
        stmt = (
            select(Category)
            .where(Category.restaurant_id == restaurant_id)
            .order_by(Category.sort_order.asc(), Category.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_category(self, category_id: uuid.UUID, restaurant_id: uuid.UUID) -> Category | None:
        stmt = select(Category).where(
            Category.id == category_id,
            Category.restaurant_id == restaurant_id,
        )
        return self.db.scalar(stmt)

    def get_category_by_slug(self, slug: str, restaurant_id: uuid.UUID) -> Category | None:
        stmt = select(Category).where(
            Category.slug == slug,
            Category.restaurant_id == restaurant_id,
        )
        return self.db.scalar(stmt)

    def list_categories_by_ids(
        self,
        category_ids: list[uuid.UUID],
        restaurant_id: uuid.UUID,
    ) -> list[Category]:
        stmt = select(Category).where(
            Category.id.in_(category_ids),
            Category.restaurant_id == restaurant_id,
        )
        return list(self.db.scalars(stmt).all())

    def add_category(self, category: Category) -> Category:
        self.db.add(category)
        self.db.flush()
        return category

    def list_products(
        self,
        restaurant_id: uuid.UUID,
        category_id: uuid.UUID | None = None,
        search: str | None = None,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Product]:
        stmt = (
            select(Product)
            .where(*self._product_conditions(
                restaurant_id=restaurant_id,
                category_id=category_id,
                search=search,
                is_active=is_active,
            ))
            .order_by(Product.sort_order.asc(), Product.name.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.scalars(stmt).all())

    def product_ids_blocked_by_required_group(
        self,
        product_ids: list[uuid.UUID],
    ) -> set[uuid.UUID]:
        """Quais destes produtos tem um grupo obrigatorio sem opcao ativa.

        UMA consulta para a pagina inteira, e nao uma por produto: a listagem
        do painel nao carrega `option_groups` justamente para nao fazer 200
        subconsultas numa tela de 200 produtos (ver `get_product_with_options`,
        que carrega, e e a tela de edicao de UM).

        `NOT EXISTS` e nao `LEFT JOIN ... HAVING count = 0`: o Postgres para na
        primeira opcao ativa que encontra, em vez de contar todas.

        ESTA CONSULTA E A MESMA REGRA DE `services/menu_rules.
        blocking_required_group`, escrita em SQL porque nao ha como
        compartilhar codigo entre as duas. As duas mudam juntas — divergindo,
        a lista do painel marca um conjunto de produtos e a tela de edicao
        marca outro.
        """
        if not product_ids:
            return set()

        tem_opcao_ativa = (
            select(ProductOption.id)
            .where(
                ProductOption.option_group_id == ProductOptionGroup.id,
                ProductOption.is_active.is_(True),
            )
            .exists()
        )
        stmt = (
            select(ProductOptionGroup.product_id)
            .where(
                ProductOptionGroup.product_id.in_(product_ids),
                ProductOptionGroup.is_active.is_(True),
                ProductOptionGroup.is_required.is_(True),
                ~tem_opcao_ativa,
            )
            .distinct()
        )
        return set(self.db.scalars(stmt).all())

    def count_products(
        self,
        restaurant_id: uuid.UUID,
        category_id: uuid.UUID | None = None,
        search: str | None = None,
        is_active: bool | None = None,
    ) -> int:
        stmt = select(func.count(Product.id)).where(*self._product_conditions(
            restaurant_id=restaurant_id,
            category_id=category_id,
            search=search,
            is_active=is_active,
        ))
        return self.db.scalar(stmt) or 0

    def list_products_by_category(
        self,
        category_id: uuid.UUID,
        restaurant_id: uuid.UUID,
    ) -> list[Product]:
        """Todos os produtos da categoria, sem paginacao.

        Existe separado de `list_products` porque a reordenacao precisa do
        conjunto INTEIRO para conferir que a lista recebida esta completa —
        e `list_products` tem `limit` com teto de 100. Reaproveita-la com um
        limite alto so esconderia o problema: a partir do 101o produto a
        conferencia passaria a aprovar uma lista incompleta e renumerar por
        cima do resto.
        """
        stmt = (
            select(Product)
            .where(
                Product.category_id == category_id,
                Product.restaurant_id == restaurant_id,
            )
            .order_by(Product.sort_order.asc(), Product.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_product(self, product_id: uuid.UUID, restaurant_id: uuid.UUID) -> Product | None:
        stmt = select(Product).where(
            Product.id == product_id,
            Product.restaurant_id == restaurant_id,
        )
        return self.db.scalar(stmt)

    def get_product_with_options(
        self,
        product_id: uuid.UUID,
        restaurant_id: uuid.UUID,
    ) -> Product | None:
        stmt = (
            select(Product)
            .options(selectinload(Product.option_groups).selectinload(ProductOptionGroup.options))
            .where(Product.id == product_id, Product.restaurant_id == restaurant_id)
        )
        return self.db.scalar(stmt)

    def get_product_by_slug(self, slug: str, restaurant_id: uuid.UUID) -> Product | None:
        stmt = select(Product).where(
            Product.slug == slug,
            Product.restaurant_id == restaurant_id,
        )
        return self.db.scalar(stmt)

    def add_product(self, product: Product) -> Product:
        self.db.add(product)
        self.db.flush()
        return product

    def list_option_groups(self, product_id: uuid.UUID) -> list[ProductOptionGroup]:
        stmt = (
            select(ProductOptionGroup)
            .options(selectinload(ProductOptionGroup.options))
            .where(ProductOptionGroup.product_id == product_id)
            .order_by(ProductOptionGroup.sort_order.asc(), ProductOptionGroup.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_option_group(
        self,
        group_id: uuid.UUID,
        restaurant_id: uuid.UUID,
    ) -> ProductOptionGroup | None:
        # A juncao com products e o que confere o restaurante: o grupo em si
        # so conhece o produto dele.
        stmt = (
            select(ProductOptionGroup)
            .join(Product, Product.id == ProductOptionGroup.product_id)
            .options(selectinload(ProductOptionGroup.options))
            .where(
                ProductOptionGroup.id == group_id,
                Product.restaurant_id == restaurant_id,
            )
        )
        return self.db.scalar(stmt)

    def add_option_group(self, group: ProductOptionGroup) -> ProductOptionGroup:
        self.db.add(group)
        self.db.flush()
        return group

    def get_option(self, option_id: uuid.UUID, restaurant_id: uuid.UUID) -> ProductOption | None:
        # Duas juncoes pelo mesmo motivo do grupo: a opcao esta a dois saltos
        # do restaurante (opcao -> grupo -> produto).
        stmt = (
            select(ProductOption)
            .join(ProductOptionGroup, ProductOptionGroup.id == ProductOption.option_group_id)
            .join(Product, Product.id == ProductOptionGroup.product_id)
            .where(
                ProductOption.id == option_id,
                Product.restaurant_id == restaurant_id,
            )
        )
        return self.db.scalar(stmt)

    def add_option(self, option: ProductOption) -> ProductOption:
        self.db.add(option)
        self.db.flush()
        return option

    @staticmethod
    def _product_conditions(
        restaurant_id: uuid.UUID,
        category_id: uuid.UUID | None,
        search: str | None,
        is_active: bool | None,
    ) -> list:
        """Filtros compartilhados pela listagem e pela contagem.

        Juntos porque um filtro que existe so em um dos dois lados devolve
        uma pagina que nao bate com o total — o mesmo arranjo de
        OrderRepository._admin_list_conditions.
        """
        conditions = [Product.restaurant_id == restaurant_id]
        if category_id is not None:
            conditions.append(Product.category_id == category_id)
        if search:
            conditions.append(_build_product_search_condition(search))
        if is_active is not None:
            conditions.append(Product.is_active.is_(is_active))
        return conditions
