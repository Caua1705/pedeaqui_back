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
)
from src.services.admin_menu_service import AdminMenuService


RESTAURANT_ID = uuid.uuid4()
OTHER_RESTAURANT_ID = uuid.uuid4()


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
        "name": "Pizzas",
        "slug": "pizzas",
        "sort_order": 0,
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_product(**overrides):
    values = {
        "id": uuid.uuid4(),
        "restaurant_id": RESTAURANT_ID,
        "category_id": uuid.uuid4(),
        "code": None,
        "name": "Pizza Calabresa",
        "slug": "pizza-calabresa",
        "description": None,
        "price": Decimal("49.90"),
        "image_path": None,
        "is_active": True,
        "is_available": True,
        "sort_order": 0,
        "option_groups": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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

    def list_categories(self, restaurant_id):
        return [item for item in self.categories if item.restaurant_id == restaurant_id]

    def get_category(self, category_id, restaurant_id):
        for item in self.categories:
            if item.id == category_id and item.restaurant_id == restaurant_id:
                return item
        return None

    def get_category_by_slug(self, slug, restaurant_id):
        for item in self.categories:
            if item.slug == slug and item.restaurant_id == restaurant_id:
                return item
        return None

    def add_category(self, category):
        return self._persist(category)

    def list_products(self, **kwargs):
        self.list_kwargs = kwargs
        return [
            item for item in self.products
            if item.restaurant_id == kwargs["restaurant_id"]
        ]

    def count_products(self, **kwargs):
        return len(self.list_products(**kwargs))

    def get_product(self, product_id, restaurant_id):
        for item in self.products:
            if item.id == product_id and item.restaurant_id == restaurant_id:
                return item
        return None

    def get_product_with_options(self, product_id, restaurant_id):
        return self.get_product(product_id, restaurant_id)

    def get_product_by_slug(self, slug, restaurant_id):
        for item in self.products:
            if item.slug == slug and item.restaurant_id == restaurant_id:
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


def build_service(repository):
    service = AdminMenuService(FakeDb())
    service.repository = repository
    return service


def scope(restaurant_id=RESTAURANT_ID, branch_id=None):
    return AdminScope(admin_user=None, restaurant_id=restaurant_id, branch_id=branch_id)


class CategoryTests(unittest.TestCase):
    def test_slug_is_derived_from_the_name(self):
        repository = TenantScopedMenuRepository()
        response = build_service(repository).create_category(
            scope(), AdminCategoryCreate(name="Pizzas Doces & Salgadas")
        )

        self.assertEqual(response.slug, "pizzas-doces-salgadas")

    def test_created_category_belongs_to_the_token_restaurant(self):
        repository = TenantScopedMenuRepository()
        build_service(repository).create_category(scope(), AdminCategoryCreate(name="Bebidas"))

        # O restaurante nao vem do corpo em lugar nenhum do contrato: se um
        # dia vier, este teste falha.
        self.assertEqual(repository.added[0].restaurant_id, RESTAURANT_ID)

    def test_duplicated_slug_in_the_same_restaurant_is_refused(self):
        repository = TenantScopedMenuRepository(categories=[make_category(slug="bebidas")])
        with self.assertRaises(HTTPException) as raised:
            build_service(repository).create_category(scope(), AdminCategoryCreate(name="Bebidas"))

        self.assertEqual(raised.exception.status_code, 409)

    def test_same_slug_in_another_restaurant_does_not_block(self):
        repository = TenantScopedMenuRepository(
            categories=[make_category(slug="bebidas", restaurant_id=OTHER_RESTAURANT_ID)]
        )
        response = build_service(repository).create_category(
            scope(), AdminCategoryCreate(name="Bebidas")
        )

        self.assertEqual(response.slug, "bebidas")

    def test_name_without_letters_or_digits_is_refused(self):
        # O slug sairia vazio e a URL publica do cardapio ficaria quebrada.
        with self.assertRaises(HTTPException) as raised:
            build_service(TenantScopedMenuRepository()).create_category(
                scope(), AdminCategoryCreate(name="🍕🍕")
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
            scope(), CategoryReorderRequest(category_ids=[self.second.id, self.first.id])
        )

        self.assertEqual((self.second.sort_order, self.first.sort_order), (0, 1))

    def test_incomplete_list_is_refused(self):
        # Renumerar so uma parte deixaria as de fora com sort_order repetido
        # e a ordem final dependeria do desempate por nome.
        with self.assertRaises(HTTPException) as raised:
            build_service(self.repository).reorder_categories(
                scope(), CategoryReorderRequest(category_ids=[self.first.id])
            )

        self.assertEqual(raised.exception.status_code, 400)

    def test_category_from_another_restaurant_is_not_found(self):
        foreign = make_category(restaurant_id=OTHER_RESTAURANT_ID)
        self.repository.categories.append(foreign)
        with self.assertRaises(HTTPException) as raised:
            build_service(self.repository).reorder_categories(
                scope(),
                CategoryReorderRequest(
                    category_ids=[self.first.id, self.second.id, foreign.id]
                ),
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_repeated_ids_are_refused_by_the_contract(self):
        category_id = uuid.uuid4()
        with self.assertRaises(ValidationError):
            CategoryReorderRequest(category_ids=[category_id, category_id])


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


if __name__ == "__main__":
    unittest.main()
