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

from sqlalchemy import func, select
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

    def list_active_categories_with_counts(
        self,
        branch_id: uuid.UUID,
        limite: int,
    ) -> list[tuple[str, int]]:
        """As categorias vendaveis DAQUELA loja, com quantos produtos cada uma tem.

        O CASO QUE PEDIU ISTO (25/08/2026). Perguntado "quais sao as
        categorias?", o atendente de voz respondeu "tem pratos como arroz, com
        varios tipos, carnes e algumas opcoes de acompanhamentos". Nao havia
        regra sobre categoria no prompt e nao havia dado nenhum chegando ate
        ele: ele descreveu um cardapio de churrascaria plausivel, que e o que
        um modelo faz quando nao tem o que ler.

        E nao daria para consertar com busca vetorial. "O que voces tem" nao se
        parece com prato nenhum — e a mesma forma do "o mais caro do cardapio"
        de `list_active_by_price`: pergunta sobre o cardapio INTEIRO nao tem
        assunto para a similaridade morder. Listar e SQL.

        O RECORTE E `Product.branch_id`, E NAO `Category.branch_id` (25/08/2026).
        Esta era a UNICA consulta do arquivo que amarrava a loja pela categoria,
        e a diferenca so e invisivel enquanto a FK composta `products
        (branch_id, category_id) -> categories (branch_id, id)` valer para toda
        linha. Amarrar pelo produto e o mesmo recorte de `get_active_products`,
        de `list_active_by_ids` e de `sellable_prices_by_id` — e quando a lista
        de categorias e a busca discordam, discordar por motivo diferente e o
        que torna o defeito impossivel de ler.

        Vale a honestidade sobre o que ESTA correcao nao provou: ela foi feita
        por inspecao, sem banco (a suite `db` precisa de Docker). Lista vazia
        contra um cardapio que existe continua tendo uma segunda explicacao
        possivel, e ela se separa desta com uma consulta so — comparar
        `count(*)` agrupado por `products.branch_id` com o mesmo agrupado por
        `categories.branch_id` na loja que falhou. Numeros diferentes acusam a
        FK; numeros iguais dizem que o defeito e outro.

        CATEGORIA VAZIA NAO ENTRA, e por isso o INNER JOIN em vez de LEFT.
        "Sobremesas" com zero produto vendavel e uma promessa que a busca
        seguinte nao cumpre: o cliente ouve a categoria, pede, e leva um "aqui
        nao temos". Contar so o vendavel (`is_active` e `is_available` do
        produto, `is_active` da categoria) faz a lista concordar com o que a
        busca acha depois.

        A CONTAGEM VIAJA JUNTO porque ela e o que transforma a lista numa
        resposta falavel. Com o numero ao lado o atendente diz "tem carnes, com
        oito opcoes" em vez de recitar doze nomes — que e justamente o teto de
        dois produtos por resposta aplicado a categoria.

        `limite` corta em `_TETO_DE_CATEGORIAS` (12). Cardapio com mais que
        isso existe, e ai a lista sai truncada de proposito: doze nomes ja sao
        mais do que cabe numa frase falada, e quem precisa do resto pergunta.
        """
        stmt = (
            select(Category.name, func.count(Product.id))
            # `select_from(Product)` explicito: sem ele o FROM sairia de
            # `Category.name`, a primeira coluna do SELECT, e o join seria de
            # `categories` para ela mesma. E o preco de contar produto e
            # mostrar categoria na mesma consulta.
            .select_from(Product)
            .join(Category, Product.category_id == Category.id)
            .where(
                Product.branch_id == branch_id,
                Category.is_active.is_(True),
                Product.is_active.is_(True),
                Product.is_available.is_(True),
            )
            .group_by(Category.id, Category.name, Category.sort_order)
            .order_by(Category.sort_order.asc(), Category.name.asc())
            .limit(limite)
        )
        return [(nome, quantos) for nome, quantos in self.db.execute(stmt).all()]
