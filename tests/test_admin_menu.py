"""Cardapio do painel (BLOCO B da Fase 3).

O que estes testes protegem, em ordem de gravidade:

1. Nenhuma categoria, produto, grupo ou opcao de OUTRO restaurante pode ser
   lido ou escrito, mesmo com o UUID em maos. O fake abaixo respeita o
   filtro por restaurante do jeito que o WHERE real respeita.
2. Slug e derivado do nome uma vez e nao muda quando o lojista renomeia:
   ele ja e URL publica divulgada.
3. A edicao parcial do grupo de opcoes e validada sobre a MESCLA com o
   banco, senao um max_select enviado sozinho cria um par impossivel.

O que fica para o teste de integracao: que o SQL de fato filtra. Aqui o
repositorio e fake — ele prova que o parametro chegou, nao que a query esta
certa.
"""

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError

from src.api.dependencies.admin_scope import AdminScope
from src.schemas.admin_menu_schema import (
    AdminCategoryCreate,
    AdminCategoryUpdate,
    AdminOptionCreate,
    AdminOptionGroupCreate,
    AdminOptionGroupUpdate,
    AdminOptionUpdate,
    AdminProductCreate,
    AdminProductUpdate,
    CategoryReorderRequest,
    ProductAvailabilityRequest,
    ProductReorderRequest,
)
from src.services.admin_menu_service import AdminMenuService
from tests import fabricas


RESTAURANT_ID = uuid.uuid4()
OTHER_RESTAURANT_ID = uuid.uuid4()

# Cardapio pende de FILIAL desde a revisao 20260820_0026. Cada restaurante
# deste arquivo tem uma loja, e o restaurante do lado tem a dele — e o que
# permite os testes de isolamento continuarem medindo o que mediam.
BRANCH_ID = uuid.uuid4()
OTHER_BRANCH_ID = uuid.uuid4()
# A segunda loja do MESMO restaurante. Existe para os testes novos: e nela
# que o mesmo slug pode se repetir e que o gerente preso a primeira nao pode
# escrever.
SECOND_BRANCH_ID = uuid.uuid4()

BRANCH_BY_RESTAURANT = {
    RESTAURANT_ID: BRANCH_ID,
    OTHER_RESTAURANT_ID: OTHER_BRANCH_ID,
}
RESTAURANT_BY_BRANCH = {
    BRANCH_ID: RESTAURANT_ID,
    SECOND_BRANCH_ID: RESTAURANT_ID,
    OTHER_BRANCH_ID: OTHER_RESTAURANT_ID,
}


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


def make_category(**overrides):
    values = {
        "id": uuid.uuid4(),
        "restaurant_id": RESTAURANT_ID,
        "branch_id": None,
        "name": "Pizzas",
        "slug": "pizzas",
        "sort_order": 0,
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**_com_filial(values))


def _com_filial(values: dict) -> dict:
    """Preenche a filial a partir do restaurante quando o teste nao a diz.

    Sem isto, todo teste antigo teria que passar `branch_id` para provar o
    que provava sobre restaurante — e os de isolamento passariam a medir
    filial em vez de tenant.
    """
    if values.get("branch_id") is None:
        values["branch_id"] = BRANCH_BY_RESTAURANT[values["restaurant_id"]]
    return values


def make_product(**overrides):
    values = {
        "id": uuid.uuid4(),
        "restaurant_id": RESTAURANT_ID,
        "branch_id": None,
        "category_id": uuid.uuid4(),
        "code": None,
        "catalog_key": None,
        "name": "Pizza Calabresa",
        "slug": "pizza-calabresa",
        "description": None,
        # NULO e o estado de TODO produto ja cadastrado: a revisao
        # 20260825_0039 acrescentou a coluna sem backfill, de proposito.
        "serves_people": None,
        "price": Decimal("49.90"),
        "image_path": None,
        "is_active": True,
        "is_available": True,
        "sort_order": 0,
        "printing_sector_id": None,
        "option_groups": [],
    }
    values.update(overrides)
    return SimpleNamespace(**_com_filial(values))


def make_option_group(**overrides):
    values = {
        "id": uuid.uuid4(),
        "product_id": uuid.uuid4(),
        "name": "Tamanho",
        "description": None,
        "min_select": 1,
        "max_select": 1,
        "is_required": True,
        "sort_order": 0,
        "is_active": True,
        "options": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_option(**overrides):
    values = {
        "id": uuid.uuid4(),
        "option_group_id": uuid.uuid4(),
        "name": "Grande",
        "description": None,
        "additional_price": Decimal("5.00"),
        "sort_order": 0,
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TenantScopedMenuRepository:
    """Fake que so devolve o que pertence ao restaurante pedido.

    E o comportamento do WHERE restaurant_id do repositorio real, e e o que
    permite provar o isolamento sem subir Postgres. Grupo e opcao chegam ao
    restaurante pelo produto, do mesmo jeito que a juncao real.
    """

    def __init__(self, categories=(), products=(), groups=(), options=()):
        self.categories = list(categories)
        self.products = list(products)
        self.groups = list(groups)
        self.options = list(options)
        self.added = []
        self.list_kwargs = None

    def _persist(self, entity):
        """Faz o que o flush do repositorio real faz: devolve a linha com id.

        O id vem de `gen_random_uuid()` no banco e chega de volta no INSERT;
        sem simular isso, a resposta da criacao nao teria id para validar.
        """
        entity.id = uuid.uuid4()
        self.added.append(entity)
        return entity

    def list_categories(self, restaurant_id, branch_id=None):
        return [
            item
            for item in self.categories
            if item.restaurant_id == restaurant_id
            and (branch_id is None or item.branch_id == branch_id)
        ]

    def get_category(self, category_id, restaurant_id):
        for item in self.categories:
            if item.id == category_id and item.restaurant_id == restaurant_id:
                return item
        return None

    def get_category_by_slug(self, slug, branch_id):
        for item in self.categories:
            if item.slug == slug and item.branch_id == branch_id:
                return item
        return None

    def add_category(self, category):
        return self._persist(category)

    def list_products(self, **kwargs):
        self.list_kwargs = kwargs
        return [
            item for item in self.products
            if item.restaurant_id == kwargs["restaurant_id"]
            and (kwargs.get("branch_id") is None or item.branch_id == kwargs["branch_id"])
        ]

    def count_products(self, **kwargs):
        return len(self.list_products(**kwargs))

    def product_ids_blocked_by_required_group(self, product_ids):
        """A mesma regra do `NOT EXISTS` do repositorio real, em Python.

        Grupo obrigatorio ATIVO sem nenhuma opcao ativa tira o produto de
        venda. O fake percorre `self.groups`/`self.options` porque e assim que
        ele ja representa a juncao — os grupos deste fake nao carregam a
        colecao `options` do ORM.
        """
        alvo = set(product_ids)
        bloqueados = set()
        for group in self.groups:
            if group.product_id not in alvo:
                continue
            if not getattr(group, "is_active", True) or not getattr(group, "is_required", False):
                continue
            ativas = [
                option
                for option in self.options
                if option.option_group_id == group.id and getattr(option, "is_active", True)
            ]
            if not ativas:
                bloqueados.add(group.product_id)
        return bloqueados

    def list_products_by_category(self, category_id, restaurant_id):
        return [
            item for item in self.products
            if item.category_id == category_id and item.restaurant_id == restaurant_id
        ]

    def get_product(self, product_id, restaurant_id):
        for item in self.products:
            if item.id == product_id and item.restaurant_id == restaurant_id:
                return item
        return None

    def get_product_with_options(self, product_id, restaurant_id):
        return self.get_product(product_id, restaurant_id)

    def get_product_by_slug(self, slug, branch_id):
        for item in self.products:
            if item.slug == slug and item.branch_id == branch_id:
                return item
        return None

    def get_product_by_catalog_key(self, catalog_key, branch_id):
        for item in self.products:
            if item.catalog_key == catalog_key and item.branch_id == branch_id:
                return item
        return None

    def add_product(self, product):
        return self._persist(product)

    def list_option_groups(self, product_id):
        return [group for group in self.groups if group.product_id == product_id]

    def get_option_group(self, group_id, restaurant_id):
        for group in self.groups:
            if group.id != group_id:
                continue
            product = next((item for item in self.products if item.id == group.product_id), None)
            if product is not None and product.restaurant_id == restaurant_id:
                return group
        return None

    def add_option_group(self, group):
        return self._persist(group)

    def get_option(self, option_id, restaurant_id):
        for option in self.options:
            if option.id != option_id:
                continue
            group = next(
                (item for item in self.groups if item.id == option.option_group_id), None
            )
            if group is None:
                return None
            product = next((item for item in self.products if item.id == group.product_id), None)
            if product is not None and product.restaurant_id == restaurant_id:
                return option
        return None

    def add_option(self, option):
        return self._persist(option)


class FakeBranchRepository:
    """Filial ativa que pertence ao restaurante do mapa. Nada mais existe.

    E o comportamento do `WHERE id = ? AND restaurant_id = ? AND is_active`
    do repositorio real — o que impede o `branch_id` do corpo de apontar para
    a loja de outro dono.
    """

    def get_active_by_id_and_restaurant(self, branch_id, restaurant_id):
        if RESTAURANT_BY_BRANCH.get(branch_id) != restaurant_id:
            return None
        return fabricas.filial(id=branch_id, restaurant_id=restaurant_id)


def build_service(repository):
    service = AdminMenuService(FakeDb())
    service.repository = repository
    service.branch_scope.branch_repository = FakeBranchRepository()
    return service


def scope(restaurant_id=RESTAURANT_ID, branch_id=None):
    return AdminScope(admin_user=None, restaurant_id=restaurant_id, branch_id=branch_id)


class CategoryTests(unittest.TestCase):
    def test_slug_is_derived_from_the_name(self):
        repository = TenantScopedMenuRepository()
        response = build_service(repository).create_category(
            scope(), AdminCategoryCreate(branch_id=BRANCH_ID, name="Pizzas Doces & Salgadas")
        )

        self.assertEqual(response.slug, "pizzas-doces-salgadas")

    def test_created_category_belongs_to_the_token_restaurant(self):
        repository = TenantScopedMenuRepository()
        build_service(repository).create_category(scope(), AdminCategoryCreate(branch_id=BRANCH_ID, name="Bebidas"))

        # O restaurante nao vem do corpo em lugar nenhum do contrato: se um
        # dia vier, este teste falha.
        self.assertEqual(repository.added[0].restaurant_id, RESTAURANT_ID)

    def test_duplicated_slug_in_the_same_restaurant_is_refused(self):
        repository = TenantScopedMenuRepository(categories=[make_category(slug="bebidas")])
        with self.assertRaises(HTTPException) as raised:
            build_service(repository).create_category(scope(), AdminCategoryCreate(branch_id=BRANCH_ID, name="Bebidas"))

        self.assertEqual(raised.exception.status_code, 409)

    def test_same_slug_in_another_restaurant_does_not_block(self):
        repository = TenantScopedMenuRepository(
            categories=[make_category(slug="bebidas", restaurant_id=OTHER_RESTAURANT_ID)]
        )
        response = build_service(repository).create_category(
            scope(), AdminCategoryCreate(branch_id=BRANCH_ID, name="Bebidas")
        )

        self.assertEqual(response.slug, "bebidas")

    def test_name_without_letters_or_digits_is_refused(self):
        # O slug sairia vazio e a URL publica do cardapio ficaria quebrada.
        with self.assertRaises(HTTPException) as raised:
            build_service(TenantScopedMenuRepository()).create_category(
                scope(), AdminCategoryCreate(branch_id=BRANCH_ID, name="🍕🍕")
            )

        self.assertEqual(raised.exception.status_code, 400)

    def test_rename_does_not_change_the_slug(self):
        category = make_category(name="Pizzas", slug="pizzas")
        repository = TenantScopedMenuRepository(categories=[category])
        build_service(repository).update_category(
            scope(), category.id, AdminCategoryUpdate(name="Pizzas especiais")
        )

        # O slug ja e URL publica divulgada; renomear nao pode quebrar link.
        self.assertEqual(category.slug, "pizzas")
        self.assertEqual(category.name, "Pizzas especiais")

    def test_partial_update_touches_only_what_was_sent(self):
        category = make_category(is_active=True, sort_order=3)
        repository = TenantScopedMenuRepository(categories=[category])
        build_service(repository).update_category(
            scope(), category.id, AdminCategoryUpdate(is_active=False)
        )

        self.assertFalse(category.is_active)
        self.assertEqual(category.sort_order, 3)

    def test_category_of_another_restaurant_is_not_found(self):
        category = make_category(restaurant_id=OTHER_RESTAURANT_ID)
        repository = TenantScopedMenuRepository(categories=[category])
        with self.assertRaises(HTTPException) as raised:
            build_service(repository).update_category(
                scope(), category.id, AdminCategoryUpdate(is_active=False)
            )

        # 404 e nao 403: um 403 confirmaria que aquele UUID existe.
        self.assertEqual(raised.exception.status_code, 404)


class CategoryReorderTests(unittest.TestCase):
    def setUp(self):
        self.first = make_category(name="A", slug="a", sort_order=0)
        self.second = make_category(name="B", slug="b", sort_order=1)
        self.repository = TenantScopedMenuRepository(categories=[self.first, self.second])

    def test_positions_follow_the_order_of_the_body(self):
        build_service(self.repository).reorder_categories(
            scope(), CategoryReorderRequest(branch_id=BRANCH_ID, category_ids=[self.second.id, self.first.id])
        )

        self.assertEqual((self.second.sort_order, self.first.sort_order), (0, 1))

    def test_incomplete_list_is_refused(self):
        # Renumerar so uma parte deixaria as de fora com sort_order repetido
        # e a ordem final dependeria do desempate por nome.
        with self.assertRaises(HTTPException) as raised:
            build_service(self.repository).reorder_categories(
                scope(), CategoryReorderRequest(branch_id=BRANCH_ID, category_ids=[self.first.id])
            )

        self.assertEqual(raised.exception.status_code, 400)

    def test_category_from_another_restaurant_is_not_found(self):
        foreign = make_category(restaurant_id=OTHER_RESTAURANT_ID)
        self.repository.categories.append(foreign)
        with self.assertRaises(HTTPException) as raised:
            build_service(self.repository).reorder_categories(
                scope(),
                CategoryReorderRequest(branch_id=BRANCH_ID, category_ids=[self.first.id, self.second.id, foreign.id]
                ),
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_repeated_ids_are_refused_by_the_contract(self):
        category_id = uuid.uuid4()
        with self.assertRaises(ValidationError):
            CategoryReorderRequest(branch_id=BRANCH_ID, category_ids=[category_id, category_id])


class ProductTests(unittest.TestCase):
    def test_product_is_created_in_a_category_of_the_same_restaurant(self):
        category = make_category()
        repository = TenantScopedMenuRepository(categories=[category])
        response = build_service(repository).create_product(
            scope(),
            AdminProductCreate(
                category_id=category.id, name="Pizza Calabresa", price=Decimal("49.90")
            ),
        )

        self.assertEqual(response.slug, "pizza-calabresa")
        self.assertEqual(repository.added[0].restaurant_id, RESTAURANT_ID)

    def test_category_of_another_restaurant_is_refused_on_create(self):
        foreign = make_category(restaurant_id=OTHER_RESTAURANT_ID)
        repository = TenantScopedMenuRepository(categories=[foreign])
        with self.assertRaises(HTTPException) as raised:
            build_service(repository).create_product(
                scope(),
                AdminProductCreate(category_id=foreign.id, name="Pizza", price=Decimal("10")),
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_price_is_stored_with_two_decimals(self):
        category = make_category()
        repository = TenantScopedMenuRepository(categories=[category])
        build_service(repository).create_product(
            scope(),
            AdminProductCreate(
                category_id=category.id, name="Refrigerante", price=Decimal("4.999")
            ),
        )

        # Numeric sem escala aceitaria 4.999 e o pedido fecharia com meio
        # centavo perdido na soma.
        self.assertEqual(repository.added[0].price, Decimal("5.00"))

    def test_moving_to_a_category_of_another_restaurant_is_refused(self):
        product = make_product()
        foreign = make_category(restaurant_id=OTHER_RESTAURANT_ID)
        repository = TenantScopedMenuRepository(categories=[foreign], products=[product])
        with self.assertRaises(HTTPException) as raised:
            build_service(repository).update_product(
                scope(), product.id, AdminProductUpdate(category_id=foreign.id)
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_product_of_another_restaurant_is_not_found(self):
        product = make_product(restaurant_id=OTHER_RESTAURANT_ID)
        repository = TenantScopedMenuRepository(products=[product])
        with self.assertRaises(HTTPException) as raised:
            build_service(repository).update_product(
                scope(), product.id, AdminProductUpdate(name="Outro nome")
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_availability_does_not_touch_is_active(self):
        product = make_product(is_active=True, is_available=True)
        repository = TenantScopedMenuRepository(products=[product])
        build_service(repository).set_product_availability(
            scope(), product.id, ProductAvailabilityRequest(is_available=False)
        )

        # "Acabou hoje" e diferente de "saiu do cardapio".
        self.assertFalse(product.is_available)
        self.assertTrue(product.is_active)

    def test_listing_is_scoped_to_the_token_restaurant(self):
        mine = make_product()
        theirs = make_product(restaurant_id=OTHER_RESTAURANT_ID)
        repository = TenantScopedMenuRepository(products=[mine, theirs])
        response = build_service(repository).list_products(scope())

        self.assertEqual([item.id for item in response.items], [mine.id])
        self.assertEqual(repository.list_kwargs["restaurant_id"], RESTAURANT_ID)

    def test_blank_search_becomes_none(self):
        repository = TenantScopedMenuRepository()
        build_service(repository).list_products(scope(), search="   ")

        self.assertIsNone(repository.list_kwargs["search"])


class OptionGroupTests(unittest.TestCase):
    def test_group_is_created_under_a_product_of_the_restaurant(self):
        product = make_product()
        repository = TenantScopedMenuRepository(products=[product])
        response = build_service(repository).create_option_group(
            scope(),
            product.id,
            AdminOptionGroupCreate(name="Tamanho", min_select=1, max_select=2, is_required=True),
        )

        self.assertEqual(response.product_id, product.id)

    def test_group_under_a_product_of_another_restaurant_is_not_found(self):
        product = make_product(restaurant_id=OTHER_RESTAURANT_ID)
        repository = TenantScopedMenuRepository(products=[product])
        with self.assertRaises(HTTPException) as raised:
            build_service(repository).create_option_group(
                scope(), product.id, AdminOptionGroupCreate(name="Tamanho")
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_required_group_needs_a_minimum(self):
        # Sem isso o pedido seria recusado na criacao sem que o cardapio
        # conseguisse explicar o que falta escolher.
        with self.assertRaises(ValidationError):
            AdminOptionGroupCreate(name="Tamanho", is_required=True, min_select=0)

    def test_max_below_min_is_refused_on_create(self):
        with self.assertRaises(ValidationError):
            AdminOptionGroupCreate(name="Adicionais", min_select=3, max_select=2)

    def test_partial_update_is_validated_against_the_stored_values(self):
        product = make_product()
        group = make_option_group(product_id=product.id, min_select=3, max_select=5)
        repository = TenantScopedMenuRepository(products=[product], groups=[group])

        with self.assertRaises(HTTPException) as raised:
            build_service(repository).update_option_group(
                scope(), group.id, AdminOptionGroupUpdate(max_select=1)
            )

        # max_select=1 e valido sozinho; invalido para um grupo que ja exige 3.
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(group.max_select, 5)

    def test_valid_partial_update_is_applied(self):
        product = make_product()
        group = make_option_group(product_id=product.id, is_active=True)
        repository = TenantScopedMenuRepository(products=[product], groups=[group])
        build_service(repository).update_option_group(
            scope(), group.id, AdminOptionGroupUpdate(is_active=False)
        )

        self.assertFalse(group.is_active)
        self.assertEqual(group.name, "Tamanho")


class OptionTests(unittest.TestCase):
    def test_option_is_created_under_a_group_of_the_restaurant(self):
        product = make_product()
        group = make_option_group(product_id=product.id)
        repository = TenantScopedMenuRepository(products=[product], groups=[group])
        response = build_service(repository).create_option(
            scope(), group.id, AdminOptionCreate(name="Grande", additional_price=Decimal("5.005"))
        )

        self.assertEqual(response.option_group_id, group.id)
        self.assertEqual(response.additional_price, 5.01)

    def test_option_of_another_restaurant_is_not_found(self):
        product = make_product(restaurant_id=OTHER_RESTAURANT_ID)
        group = make_option_group(product_id=product.id)
        option = make_option(option_group_id=group.id)
        repository = TenantScopedMenuRepository(
            products=[product], groups=[group], options=[option]
        )

        with self.assertRaises(HTTPException) as raised:
            build_service(repository).update_option(
                scope(), option.id, AdminOptionUpdate(is_active=False)
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_option_is_deactivated_instead_of_deleted(self):
        product = make_product()
        group = make_option_group(product_id=product.id)
        option = make_option(option_group_id=group.id, is_active=True)
        repository = TenantScopedMenuRepository(
            products=[product], groups=[group], options=[option]
        )
        response = build_service(repository).update_option(
            scope(), option.id, AdminOptionUpdate(is_active=False)
        )

        # order_item_options aponta para esta linha por FK: apagar quebraria
        # o historico do pedido que o cliente ainda consulta.
        self.assertFalse(response.is_active)


class ProductReorderTests(unittest.TestCase):
    """A reordenacao e por CATEGORIA, nao pelo restaurante inteiro.

    `sort_order` de produto so significa alguma coisa dentro da categoria: o
    cardapio publico ordena por categoria e SO ENTAO por produto. Renumerar
    numa sequencia unica do restaurante faria a posicao de um produto dentro
    da categoria depender de quantos produtos vieram antes dela na lista.
    """

    def setUp(self):
        self.category = make_category()
        self.other_category = make_category(name="Bebidas", slug="bebidas")
        self.first = make_product(category_id=self.category.id, name="A", sort_order=0)
        self.second = make_product(category_id=self.category.id, name="B", sort_order=1)
        self.repository = TenantScopedMenuRepository(
            categories=[self.category, self.other_category],
            products=[self.first, self.second],
        )

    def test_new_order_is_written_as_sequential_positions(self):
        build_service(self.repository).reorder_products(
            scope(),
            ProductReorderRequest(
                category_id=self.category.id,
                product_ids=[self.second.id, self.first.id],
            ),
        )

        self.assertEqual((self.second.sort_order, self.first.sort_order), (0, 1))

    def test_response_comes_back_in_the_requested_order(self):
        response = build_service(self.repository).reorder_products(
            scope(),
            ProductReorderRequest(
                category_id=self.category.id,
                product_ids=[self.second.id, self.first.id],
            ),
        )

        self.assertEqual([item.id for item in response], [self.second.id, self.first.id])

    def test_incomplete_list_is_refused(self):
        # Renumerar so uma parte deixaria os de fora com sort_order repetido,
        # e a ordem final dependeria do desempate por nome.
        with self.assertRaises(HTTPException) as raised:
            build_service(self.repository).reorder_products(
                scope(),
                ProductReorderRequest(
                    category_id=self.category.id, product_ids=[self.first.id]
                ),
            )

        self.assertEqual(raised.exception.status_code, 400)

    def test_product_from_another_category_is_not_found(self):
        outsider = make_product(category_id=self.other_category.id, name="Coca")
        self.repository.products.append(outsider)

        with self.assertRaises(HTTPException) as raised:
            build_service(self.repository).reorder_products(
                scope(),
                ProductReorderRequest(
                    category_id=self.category.id,
                    product_ids=[self.first.id, self.second.id, outsider.id],
                ),
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_category_from_another_restaurant_is_not_found(self):
        foreign = make_category(restaurant_id=OTHER_RESTAURANT_ID)
        self.repository.categories.append(foreign)

        with self.assertRaises(HTTPException) as raised:
            build_service(self.repository).reorder_products(
                scope(),
                ProductReorderRequest(
                    category_id=foreign.id, product_ids=[uuid.uuid4()]
                ),
            )

        # 404 e nao 400: responder "envie todos os produtos" contaria ao
        # chamador que aquela categoria existe em algum restaurante.
        self.assertEqual(raised.exception.status_code, 404)

    def test_product_from_another_restaurant_is_not_found(self):
        foreign = make_product(
            restaurant_id=OTHER_RESTAURANT_ID, category_id=self.category.id
        )
        self.repository.products.append(foreign)

        with self.assertRaises(HTTPException) as raised:
            build_service(self.repository).reorder_products(
                scope(),
                ProductReorderRequest(
                    category_id=self.category.id,
                    product_ids=[self.first.id, self.second.id, foreign.id],
                ),
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_reordering_one_category_does_not_touch_another(self):
        outsider = make_product(
            category_id=self.other_category.id, name="Coca", sort_order=7
        )
        self.repository.products.append(outsider)

        build_service(self.repository).reorder_products(
            scope(),
            ProductReorderRequest(
                category_id=self.category.id,
                product_ids=[self.second.id, self.first.id],
            ),
        )

        self.assertEqual(outsider.sort_order, 7)

    def test_repeated_ids_are_refused_by_the_contract(self):
        product_id = uuid.uuid4()
        with self.assertRaises(ValidationError):
            ProductReorderRequest(
                category_id=uuid.uuid4(), product_ids=[product_id, product_id]
            )


if __name__ == "__main__":
    unittest.main()


class UnavailableByRequiredGroupTests(unittest.TestCase):
    """O sinal que diz ao lojista POR QUE o produto sumiu do cardapio.

    Um grupo obrigatorio ativo sem nenhuma opcao ativa tira o produto de
    venda. No painel, `is_active` e `is_available` continuam ligados — sem
    este campo o produto some do cliente e nada explica.

    A listagem e a tela de edicao chegam a resposta por CAMINHOS DIFERENTES: a
    listagem por uma consulta agregada (uma para a pagina inteira), a edicao
    lendo os grupos ja carregados. Os dois estao testados porque e entre eles
    que a divergencia apareceria.
    """

    def _produto_bloqueado(self):
        product = make_product()
        group = make_option_group(product_id=product.id, is_required=True, is_active=True)
        option = make_option(option_group_id=group.id, is_active=False)
        return product, group, option

    def test_the_listing_marks_the_blocked_product(self):
        product, group, option = self._produto_bloqueado()
        saudavel = make_product(name="Coca", slug="coca")
        repository = TenantScopedMenuRepository(
            products=[product, saudavel], groups=[group], options=[option]
        )

        items = build_service(repository).list_products(scope()).items

        por_nome = {item.name: item for item in items}
        self.assertTrue(por_nome["Pizza Calabresa"].unavailable_by_required_group)
        self.assertFalse(por_nome["Coca"].unavailable_by_required_group)

    def test_the_listing_asks_the_database_once_for_the_whole_page(self):
        """A consulta agregada existe para a listagem nao virar uma leitura
        por produto — a tela tem 200 deles."""
        product, group, option = self._produto_bloqueado()
        repository = TenantScopedMenuRepository(
            products=[product], groups=[group], options=[option]
        )
        chamadas = []
        original = repository.product_ids_blocked_by_required_group

        def contando(product_ids):
            chamadas.append(list(product_ids))
            return original(product_ids)

        repository.product_ids_blocked_by_required_group = contando

        build_service(repository).list_products(scope())

        self.assertEqual(len(chamadas), 1)

    def test_the_edit_screen_marks_it_too(self):
        product, group, option = self._produto_bloqueado()
        group.options = [option]
        product.option_groups = [group]
        repository = TenantScopedMenuRepository(
            products=[product], groups=[group], options=[option]
        )

        response = build_service(repository).get_product(scope(), product.id)

        self.assertTrue(response.unavailable_by_required_group)

    def test_a_healthy_product_is_not_marked(self):
        product = make_product()
        group = make_option_group(product_id=product.id, is_required=True)
        option = make_option(option_group_id=group.id, is_active=True)
        group.options = [option]
        product.option_groups = [group]
        repository = TenantScopedMenuRepository(
            products=[product], groups=[group], options=[option]
        )

        response = build_service(repository).get_product(scope(), product.id)

        self.assertFalse(response.unavailable_by_required_group)

    def test_turning_the_last_option_back_on_clears_the_flag(self):
        """O caminho de volta: o lojista reativa uma opcao e o produto volta a
        venda. Se o campo nao limpasse, ele ficaria marcado para sempre."""
        product = make_product()
        group = make_option_group(product_id=product.id, is_required=True)
        option = make_option(option_group_id=group.id, is_active=False)
        group.options = [option]
        product.option_groups = [group]
        repository = TenantScopedMenuRepository(
            products=[product], groups=[group], options=[option]
        )
        service = build_service(repository)

        self.assertTrue(service.get_product(scope(), product.id).unavailable_by_required_group)

        option.is_active = True

        self.assertFalse(service.get_product(scope(), product.id).unavailable_by_required_group)


class EscopoPorFilialTests(unittest.TestCase):
    """O que a revisao 20260820_0026 tornou EXPRIMIVEL.

    A decisao 2 do `admin_menu_service.py` dizia, com razao, que cardapio nao
    tinha filial e que por isso o gerente preso a uma loja editava a rede
    inteira. Aquilo era limitacao de schema descrita como decisao: nao havia
    coluna para restringir.

    Agora ha, e todo caminho de leitura e escrita passa pelo `AdminScope`.
    **404 e nunca 403**, pela mesma regra do resto do painel: um 403
    confirmaria que a filial do lado existe.
    """

    def _preso_a_matriz(self):
        return scope(branch_id=BRANCH_ID)

    def test_a_listagem_de_produtos_do_dono_traz_as_duas_lojas(self):
        repository = TenantScopedMenuRepository(products=[
            make_product(name="Costela"),
            make_product(name="Tapioca", branch_id=SECOND_BRANCH_ID),
        ])

        pagina = build_service(repository).list_products(scope())

        self.assertEqual(
            {item.branch_id for item in pagina.items}, {BRANCH_ID, SECOND_BRANCH_ID}
        )

    def test_o_gerente_preso_a_uma_filial_ve_so_a_dele(self):
        repository = TenantScopedMenuRepository(products=[
            make_product(name="Costela"),
            make_product(name="Tapioca", branch_id=SECOND_BRANCH_ID),
        ])

        pagina = build_service(repository).list_products(self._preso_a_matriz())

        self.assertEqual([item.name for item in pagina.items], ["Costela"])

    def test_o_filtro_da_querystring_so_restringe(self):
        """Preso a matriz e pedindo a outra: 404, nao a lista alheia."""
        repository = TenantScopedMenuRepository()

        with self.assertRaises(HTTPException) as raised:
            build_service(repository).list_products(
                self._preso_a_matriz(), branch_id=SECOND_BRANCH_ID
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_editar_produto_de_outra_filial_e_404(self):
        de_outra_loja = make_product(branch_id=SECOND_BRANCH_ID)
        repository = TenantScopedMenuRepository(products=[de_outra_loja])

        with self.assertRaises(HTTPException) as raised:
            build_service(repository).update_product(
                self._preso_a_matriz(), de_outra_loja.id, AdminProductUpdate(name="Novo")
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_o_grupo_de_opcoes_chega_a_filial_pelo_produto(self):
        """Grupo nao tem coluna de filial: ele chega nela pelo produto.

        Sem a reconferencia, o UUID de um grupo bastaria para o gerente da
        matriz editar os complementos da picanha da outra loja.
        """
        de_outra_loja = make_product(branch_id=SECOND_BRANCH_ID)
        grupo = make_option_group(product_id=de_outra_loja.id)
        repository = TenantScopedMenuRepository(products=[de_outra_loja], groups=[grupo])

        with self.assertRaises(HTTPException) as raised:
            build_service(repository).update_option_group(
                self._preso_a_matriz(), grupo.id, AdminOptionGroupUpdate(name="Ponto")
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_a_opcao_chega_a_filial_por_dois_saltos(self):
        de_outra_loja = make_product(branch_id=SECOND_BRANCH_ID)
        grupo = make_option_group(product_id=de_outra_loja.id)
        opcao = make_option(option_group_id=grupo.id)
        repository = TenantScopedMenuRepository(
            products=[de_outra_loja], groups=[grupo], options=[opcao]
        )

        with self.assertRaises(HTTPException) as raised:
            build_service(repository).update_option(
                self._preso_a_matriz(), opcao.id, AdminOptionUpdate(name="Bem passado")
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_criar_categoria_em_filial_de_outro_restaurante_e_404(self):
        repository = TenantScopedMenuRepository()

        with self.assertRaises(HTTPException) as raised:
            build_service(repository).create_category(
                scope(), AdminCategoryCreate(branch_id=OTHER_BRANCH_ID, name="Bebidas")
            )

        self.assertEqual(raised.exception.status_code, 404)


class SlugPorFilialTests(unittest.TestCase):
    """O 409 passou a ser por FILIAL, e e o que permite as duas lojas
    venderem "Picanha" sem uma delas virar `picanha-2` na URL."""

    def test_o_mesmo_slug_na_outra_filial_nao_bloqueia(self):
        repository = TenantScopedMenuRepository(
            categories=[make_category(slug="bebidas", branch_id=SECOND_BRANCH_ID)]
        )

        resposta = build_service(repository).create_category(
            scope(), AdminCategoryCreate(branch_id=BRANCH_ID, name="Bebidas")
        )

        self.assertEqual(resposta.slug, "bebidas")
        self.assertEqual(resposta.branch_id, BRANCH_ID)

    def test_o_mesmo_slug_na_mesma_filial_continua_409(self):
        repository = TenantScopedMenuRepository(
            categories=[make_category(slug="bebidas", branch_id=BRANCH_ID)]
        )

        with self.assertRaises(HTTPException) as raised:
            build_service(repository).create_category(
                scope(), AdminCategoryCreate(branch_id=BRANCH_ID, name="Bebidas")
            )

        self.assertEqual(raised.exception.status_code, 409)


class ChaveDeCatalogoTests(unittest.TestCase):
    """Repetir a chave ENTRE lojas e o uso; dentro de uma e o defeito.

    Duas linhas da mesma loja com a mesma chave fariam o relatorio contar a
    mesma venda duas vezes — o oposto do que a coluna existe para fazer.
    """

    def _categoria_e_repositorio(self, produtos=()):
        categoria = make_category()
        return categoria, TenantScopedMenuRepository(
            categories=[categoria], products=list(produtos)
        )

    def test_a_chave_repetida_na_mesma_filial_e_409(self):
        ja_existe = make_product(catalog_key="picanha")
        categoria, repository = self._categoria_e_repositorio([ja_existe])

        with self.assertRaises(HTTPException) as raised:
            build_service(repository).create_product(
                scope(),
                AdminProductCreate(
                    category_id=categoria.id,
                    name="Picanha Premium",
                    price=Decimal("120.00"),
                    catalog_key="picanha",
                ),
            )

        self.assertEqual(raised.exception.status_code, 409)

    def test_a_chave_repetida_em_outra_filial_passa(self):
        na_outra_loja = make_product(catalog_key="picanha", branch_id=SECOND_BRANCH_ID)
        categoria, repository = self._categoria_e_repositorio([na_outra_loja])

        criado = build_service(repository).create_product(
            scope(),
            AdminProductCreate(
                category_id=categoria.id,
                name="Picanha",
                price=Decimal("89.90"),
                catalog_key="picanha",
            ),
        )

        self.assertEqual(criado.catalog_key, "picanha")

    def test_a_chave_nula_nao_colide_com_nada(self):
        """E o estado normal do produto sem par em outra loja — por isso o
        indice unico e parcial."""
        sem_chave = make_product(catalog_key=None)
        categoria, repository = self._categoria_e_repositorio([sem_chave])

        criado = build_service(repository).create_product(
            scope(),
            AdminProductCreate(
                category_id=categoria.id, name="Fraldinha", price=Decimal("60.00")
            ),
        )

        self.assertIsNone(criado.catalog_key)

    def test_editar_o_proprio_produto_nao_colide_consigo_mesmo(self):
        produto = make_product(catalog_key="picanha")
        repository = TenantScopedMenuRepository(
            categories=[make_category()], products=[produto]
        )

        atualizado = build_service(repository).update_product(
            scope(), produto.id, AdminProductUpdate(catalog_key="picanha")
        )

        self.assertEqual(atualizado.catalog_key, "picanha")


class ProdutoNaoMudaDeFilialTests(unittest.TestCase):
    def test_a_filial_do_produto_vem_da_categoria(self):
        """Nao ha `branch_id` em `AdminProductCreate`: a categoria ja diz a loja.

        Pedir os dois abriria a chance de virem em desacordo, e a FK composta
        recusaria a gravacao com um 500 de IntegrityError.
        """
        categoria = make_category(branch_id=SECOND_BRANCH_ID)
        repository = TenantScopedMenuRepository(categories=[categoria])

        criado = build_service(repository).create_product(
            scope(),
            AdminProductCreate(
                category_id=categoria.id, name="Tapioca", price=Decimal("18.00")
            ),
        )

        self.assertEqual(criado.branch_id, SECOND_BRANCH_ID)

    def test_mover_para_categoria_de_outra_filial_e_400(self):
        """400 com frase, e nao o 500 da violacao de FK.

        Mover a linha levaria junto os grupos de opcao, o setor de impressao e
        a chave de catalogo, e deixaria o historico de pedido apontando para
        um produto que aquela loja nao vende mais.
        """
        produto = make_product()
        da_outra_loja = make_category(branch_id=SECOND_BRANCH_ID)
        repository = TenantScopedMenuRepository(
            categories=[da_outra_loja], products=[produto]
        )

        with self.assertRaises(HTTPException) as raised:
            build_service(repository).update_product(
                scope(), produto.id, AdminProductUpdate(category_id=da_outra_loja.id)
            )

        self.assertEqual(raised.exception.status_code, 400)

    def test_mover_dentro_da_mesma_filial_continua_valendo(self):
        produto = make_product()
        outra_secao = make_category(slug="promocoes")
        repository = TenantScopedMenuRepository(
            categories=[outra_secao], products=[produto]
        )

        atualizado = build_service(repository).update_product(
            scope(), produto.id, AdminProductUpdate(category_id=outra_secao.id)
        )

        self.assertEqual(atualizado.category_id, outra_secao.id)


class ReordenacaoPorFilialTests(unittest.TestCase):
    def test_a_lista_completa_exigida_e_a_da_FILIAL(self):
        """O conjunto que compartilha a numeracao passou a ser a loja.

        Medindo o restaurante, o dono com duas lojas nunca conseguiria
        reordenar uma sem mandar as categorias da outra junto.
        """
        da_matriz = make_category(slug="carnes")
        da_outra = make_category(slug="bebidas", branch_id=SECOND_BRANCH_ID)
        repository = TenantScopedMenuRepository(categories=[da_matriz, da_outra])

        resposta = build_service(repository).reorder_categories(
            scope(),
            CategoryReorderRequest(branch_id=BRANCH_ID, category_ids=[da_matriz.id]),
        )

        self.assertEqual([item.id for item in resposta], [da_matriz.id])
        self.assertEqual(da_matriz.sort_order, 0)

