"""Consultas do cardapio publico, por FILIAL.

Todas recebem `branch_id` e nao `restaurant_id` desde a revisao
20260820_0026: produto pertence a uma loja, e cada loja tem os proprios
precos e a propria disponibilidade. Consultar por restaurante devolveria a
picanha das duas lojas — com dois precos, e sem nada na resposta dizendo qual
e de quem.

E o mesmo motivo pelo qual `OrderService` e o Rapi passaram a chamar daqui
com a filial: era por este caminho que o cliente fechava pedido na filial B
com produto (e preco) da filial A.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.models.category_model import Category
from src.models.product_model import Product
from src.models.product_option_model import ProductOptionGroup


class ProductRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_active_by_category_slug(self, branch_id: uuid.UUID, category_slug: str) -> list[Product]:
        stmt = (
            select(Product)
            .join(Category, Product.category_id == Category.id)
            .options(selectinload(Product.option_groups).selectinload(ProductOptionGroup.options))
            .where(
                Product.branch_id == branch_id,
                Category.slug == category_slug,
                Category.is_active.is_(True),
                Product.is_active.is_(True),
                Product.is_available.is_(True),
            )
            .order_by(Product.sort_order.asc(), Product.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def active_category_exists(self, branch_id: uuid.UUID, category_slug: str) -> bool:
        stmt = select(Category.id).where(
            Category.branch_id == branch_id,
            Category.slug == category_slug,
            Category.is_active.is_(True),
        )
        return self.db.scalar(stmt) is not None

    def get_active_by_slug(self, branch_id: uuid.UUID, product_slug: str) -> Product | None:
        """O produto por slug DENTRO de uma filial.

        O slug e unico por `(branch_id, slug)` desde a revisao 20260820_0026,
        entao esta consulta volta a ter no maximo uma linha. Por restaurante
        ela passaria a devolver uma linha por loja, e o `scalar()` escolheria
        uma sem criterio nenhum — o link publico do produto abriria o preco de
        uma loja qualquer.
        """
        stmt = (
            select(Product)
            .join(Category, Product.category_id == Category.id)
            .options(selectinload(Product.option_groups).selectinload(ProductOptionGroup.options))
            .where(
                Product.branch_id == branch_id,
                Product.slug == product_slug,
                Product.is_active.is_(True),
                Product.is_available.is_(True),
                Category.is_active.is_(True),
            )
        )
        return self.db.scalar(stmt)

    def sellable_prices_by_id(
        self,
        branch_id: uuid.UUID,
        product_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, Decimal]:
        """So o preco vigente, por id. Consulta de chave primaria, sem opcao junto.

        Existe para que o preco que vai ao MODELO saia da linha viva de
        `products` a cada requisicao, e nunca do cache de busca de 20 minutos
        — ver `RetrievalService._with_current_prices`.

        Os filtros sao os MESMOS de `list_active_by_ids`, e nao por simetria:
        e o que garante que todo produto que chega ao modelo consegue virar
        cartao depois. Sem eles, um produto marcado como indisponivel enquanto
        estava no cache seria recomendado no texto e sumiria na hidratacao —
        exatamente a resposta com produto no texto e `products` vazio.

        `branch_id` entrou junto com o cardapio por filial. Sem ele, o Rapi
        carimbava preco de uma loja em produto de outra: o cache de busca
        guarda ids, e um id de outra filial passava por aqui sem nada
        recusa-lo.
        """
        if not product_ids:
            return {}

        stmt = select(Product.id, Product.price).where(
            Product.branch_id == branch_id,
            Product.id.in_(product_ids),
            Product.is_active.is_(True),
            Product.is_available.is_(True),
        )
        return {row.id: row.price for row in self.db.execute(stmt)}

    def list_active_by_ids(self, branch_id: uuid.UUID, product_ids: list[uuid.UUID]) -> list[Product]:
        """Os produtos vendaveis DAQUELA loja, por id.

        E a barreira que faz `POST /orders` recusar produto da filial A num
        pedido da filial B: `_get_valid_products` compara o tamanho do
        resultado com o dos ids pedidos e responde 400 quando falta alguem.
        Por restaurante, o produto da outra loja voltava daqui e o pedido
        passava.
        """
        stmt = (
            select(Product)
            .options(selectinload(Product.option_groups).selectinload(ProductOptionGroup.options))
            .where(
                Product.branch_id == branch_id,
                Product.id.in_(product_ids),
                Product.is_active.is_(True),
                Product.is_available.is_(True),
            )
        )
        return list(self.db.scalars(stmt).all())

    def list_active_by_price(
        self,
        branch_id: uuid.UUID,
        crescente: bool,
        limite: int,
    ) -> list[Product]:
        """Os vendaveis DAQUELA loja ordenados por preco. O caminho do superlativo.

        POR QUE ISTO NAO E BUSCA VETORIAL (25/08/2026). "Manda o mais caro do
        cardapio" nao tem resposta na busca por significado, e o motivo e
        estrutural: ela devolve os N mais PARECIDOS com a pergunta, e o mais
        caro da casa nao tem nenhuma razao para se parecer com a palavra
        "cardapio". Aumentar o `top_k` nao conserta — so torna o acaso mais
        provavel.

        Superlativo sobre o cardapio inteiro e ordenacao, e ordenacao e SQL.

        `price` daqui e o preco vigente da FILIAL, e nao ha o descompasso que
        `RetrievalService._with_current_prices` existe para corrigir: la o
        numero vem do indice, que envelhece; aqui vem da linha viva. Ordenar
        pelo numero do indice devolveria "o mais barato" errado sem erro e sem
        log — o pior formato de defeito que este repositorio conhece.

        Sem `Decimal` nulo na ordenacao: preco ausente nao e preco zero, e um
        produto sem valor no topo de "o mais barato" seria o atendente
        oferecendo de graca o que ninguem precificou.
        """
        stmt = (
            select(Product)
            .options(selectinload(Product.option_groups).selectinload(ProductOptionGroup.options))
            .where(
                Product.branch_id == branch_id,
                Product.is_active.is_(True),
                Product.is_available.is_(True),
                Product.price.is_not(None),
                Product.price > 0,
            )
            .order_by(Product.price.asc() if crescente else Product.price.desc())
            .limit(limite)
        )
        return list(self.db.scalars(stmt).all())
