"""Caracterizacao de `services/menu_service.py` — o cardapio publico.

TESTE DE CARACTERIZACAO: descreve o que o codigo faz HOJE. Comportamento
esquisito fica registrado e verde, com comentario apontando o problema.

Este e o servico que monta a vitrine que o CLIENTE FINAL ve. Ele nao tinha
teste nenhum (`test_admin_menu.py` e do outro lado, o do painel), e o que ele
faz de mais delicado nao e chamar repositorio: e a ORDEM e o FILTRO dos grupos
de adicionais dentro de `product_response` — hoje escritos como uma list
comprehension dentro de outra, com `sorted` de generator em cada nivel.

Congelar esse comportamento e o que permite desmontar aquela comprehension
depois sem adivinhar se a ordem mudou.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.services.menu_service import MenuService


RESTAURANT_ID = uuid.uuid4()


class FakeDb:
    """O MenuService so guarda a sessao para entregar aos repositorios, que
    sao trocados por fakes logo em seguida. Nada aqui e chamado."""


class FakeMenuRepository:
    def __init__(self, settings=None, branches=(), banners=None, coupons=(), categories=(), products=()):
        self.settings = settings
        self.branches = list(branches)
        self.banners = banners or {}
        self.coupons = list(coupons)
        self.categories = list(categories)
        self.products = list(products)
        self.banner_types_asked = []

    def get_settings(self, restaurant_id):
        return self.settings

    def get_active_branches(self, restaurant_id):
        return self.branches

    def get_banners_by_type(self, restaurant_id, banner_type):
        self.banner_types_asked.append(banner_type)
        return self.banners.get(banner_type, [])

    def get_active_coupons(self, restaurant_id):
        return self.coupons

    def get_active_categories(self, restaurant_id):
        return self.categories

    def get_active_products(self, restaurant_id):
        return self.products


class FakeProductRepository:
    def __init__(self, category_exists=True, by_category=(), by_slug=None):
        self.category_exists = category_exists
        self.by_category = list(by_category)
        self.by_slug = by_slug

    def active_category_exists(self, restaurant_id, category_slug):
        return self.category_exists

    def list_active_by_category_slug(self, restaurant_id, category_slug):
        return self.by_category

    def get_active_by_slug(self, restaurant_id, product_slug):
        return self.by_slug


class FakeRestaurantService:
    def __init__(self, restaurant=None):
        self.restaurant = restaurant or make_restaurant()
        self.slugs_asked = []

    def get_active_restaurant(self, restaurant_slug):
        self.slugs_asked.append(restaurant_slug)
        return self.restaurant

    def to_public_response(self, restaurant):
        return restaurant


# ---------------------------------------------------------------------------
# Construtores de linha
# ---------------------------------------------------------------------------


def make_restaurant():
    return SimpleNamespace(id=RESTAURANT_ID, name="Pizzaria do Ze", slug="pizzaria-do-ze")


def make_option(name, sort_order=0, is_active=True, additional_price="2.50"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        description=None,
        additional_price=Decimal(additional_price),
        sort_order=sort_order,
        is_active=is_active,
    )


def make_group(name, options=(), sort_order=0, is_active=True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        description=None,
        min_select=0,
        max_select=1,
        is_required=False,
        sort_order=sort_order,
        is_active=is_active,
        options=list(options),
    )


def make_product(name="X-Burger", slug="x-burger", price="24.90", image_path=None, option_groups=()):
    return SimpleNamespace(
        id=uuid.uuid4(),
        restaurant_id=RESTAURANT_ID,
        category_id=uuid.uuid4(),
        code="X1",
        name=name,
        slug=slug,
        description=None,
        price=Decimal(price),
        image_path=image_path,
        is_active=True,
        is_available=True,
        sort_order=0,
        option_groups=list(option_groups),
    )


def make_service(menu_repository=None, product_repository=None, restaurant_service=None):
    service = MenuService(FakeDb())
    service.menu_repository = menu_repository or FakeMenuRepository()
    service.product_repository = product_repository or FakeProductRepository()
    service.restaurant_service = restaurant_service or FakeRestaurantService()
    return service


# ---------------------------------------------------------------------------
# product_response — a ordem e o filtro dos adicionais
# ---------------------------------------------------------------------------


class TestProductResponseFiltering:
    def test_inactive_groups_do_not_appear(self):
        product = make_product(
            option_groups=[
                make_group("Ativo", [make_option("a")]),
                make_group("Inativo", [make_option("b")], is_active=False),
            ]
        )
        assert [group.name for group in MenuService.product_response(product).option_groups] == ["Ativo"]

    def test_inactive_options_do_not_appear(self):
        product = make_product(
            option_groups=[
                make_group("Grupo", [make_option("fica"), make_option("sai", is_active=False)])
            ]
        )
        groups = MenuService.product_response(product).option_groups
        assert [option.name for option in groups[0].options] == ["fica"]

    def test_a_group_whose_options_are_all_inactive_still_appears_empty(self):
        """ESQUISITO, e registrado como esta.

        O filtro de grupo e o de opcao sao independentes: um grupo ATIVO cujas
        opcoes estao todas inativas continua saindo no cardapio, com a lista
        de opcoes vazia. Se ele for obrigatorio (`is_required`), o cliente ve
        um passo que nao tem o que escolher.

        Nao e corrigido aqui: decidir se o grupo some ou se vira erro de
        configuracao e decisao separada.
        """
        product = make_product(
            option_groups=[make_group("Vazio", [make_option("x", is_active=False)])]
        )
        groups = MenuService.product_response(product).option_groups
        assert len(groups) == 1
        assert groups[0].options == []


class TestProductResponseOrdering:
    def test_groups_come_in_sort_order(self):
        product = make_product(
            option_groups=[
                make_group("Terceiro", sort_order=2),
                make_group("Primeiro", sort_order=0),
                make_group("Segundo", sort_order=1),
            ]
        )
        nomes = [group.name for group in MenuService.product_response(product).option_groups]
        assert nomes == ["Primeiro", "Segundo", "Terceiro"]

    def test_the_name_breaks_a_tie_in_sort_order(self):
        """Sem o desempate por nome, dois grupos com o mesmo `sort_order`
        sairiam na ordem que o banco devolvesse — e a mesma tela mudaria de
        ordem entre duas cargas."""
        product = make_product(
            option_groups=[
                make_group("Zeta", sort_order=1),
                make_group("Alfa", sort_order=1),
            ]
        )
        nomes = [group.name for group in MenuService.product_response(product).option_groups]
        assert nomes == ["Alfa", "Zeta"]

    def test_a_null_sort_order_counts_as_zero_and_comes_first(self):
        """`option.sort_order or 0`. Linha antiga sem `sort_order` preenchido
        vai para o comeco, nao para o fim."""
        product = make_product(
            option_groups=[
                make_group("Com ordem", sort_order=1),
                make_group("Sem ordem", sort_order=None),
            ]
        )
        nomes = [group.name for group in MenuService.product_response(product).option_groups]
        assert nomes == ["Sem ordem", "Com ordem"]

    def test_options_follow_the_same_two_rules(self):
        product = make_product(
            option_groups=[
                make_group(
                    "Grupo",
                    [
                        make_option("Zeta", sort_order=1),
                        make_option("Alfa", sort_order=1),
                        make_option("Sem ordem", sort_order=None),
                    ],
                )
            ]
        )
        groups = MenuService.product_response(product).option_groups
        assert [option.name for option in groups[0].options] == ["Sem ordem", "Alfa", "Zeta"]


class TestProductResponseValues:
    def test_money_comes_out_as_float(self):
        """A resposta publica serializa dinheiro como float. O `Decimal` vale
        no calculo; aqui ja e saida."""
        product = make_product(price="24.90", option_groups=[make_group("G", [make_option("o", additional_price="2.50")])])
        response = MenuService.product_response(product)
        assert response.price == 24.90
        assert isinstance(response.price, float)
        assert response.option_groups[0].options[0].additional_price == 2.50

    def test_no_image_means_no_url(self):
        assert MenuService.product_response(make_product(image_path=None)).image_url is None

    def test_an_image_path_becomes_a_url(self):
        response = MenuService.product_response(make_product(image_path="produtos/x.jpg"))
        assert response.image_url is not None
        assert response.image_url.endswith("produtos/x.jpg")


# ---------------------------------------------------------------------------
# As tres rotas publicas
# ---------------------------------------------------------------------------


class TestGetProductsByCategory:
    def test_it_returns_the_products_of_the_category(self):
        service = make_service(
            product_repository=FakeProductRepository(by_category=[make_product(name="A"), make_product(name="B")])
        )
        assert [p.name for p in service.get_products_by_category("pizzaria-do-ze", "lanches")] == ["A", "B"]

    def test_a_category_that_does_not_exist_is_404(self):
        service = make_service(product_repository=FakeProductRepository(category_exists=False))
        with pytest.raises(HTTPException) as exc:
            service.get_products_by_category("pizzaria-do-ze", "nao-existe")
        assert exc.value.status_code == 404

    def test_an_empty_category_is_an_empty_list_not_a_404(self):
        """Categoria que existe e esta vazia devolve `[]`. E o 404 fica
        reservado para categoria que nao existe — as duas coisas sao
        diferentes para quem consome a vitrine."""
        service = make_service(product_repository=FakeProductRepository(category_exists=True, by_category=[]))
        assert service.get_products_by_category("pizzaria-do-ze", "lanches") == []


class TestGetProductDetail:
    def test_it_returns_the_product(self):
        service = make_service(product_repository=FakeProductRepository(by_slug=make_product(name="X-Salada")))
        assert service.get_product_detail("pizzaria-do-ze", "x-salada").name == "X-Salada"

    def test_a_product_that_does_not_exist_is_404(self):
        service = make_service(product_repository=FakeProductRepository(by_slug=None))
        with pytest.raises(HTTPException) as exc:
            service.get_product_detail("pizzaria-do-ze", "nao-existe")
        assert exc.value.status_code == 404


class TestGetRestaurantMenu:
    def test_it_asks_for_hero_and_highlight_banners_separately(self):
        """Duas leituras de banner com o mesmo repositorio, distinguidas pelo
        tipo. Se alguem trocar a ordem das duas chamadas, o cardapio troca as
        faixas de lugar sem erro nenhum."""
        menu_repository = FakeMenuRepository()
        service = make_service(menu_repository=menu_repository)

        service.get_restaurant_menu("pizzaria-do-ze")

        assert menu_repository.banner_types_asked == ["hero", "highlight"]

    def test_each_banner_type_lands_in_its_own_field(self):
        hero = SimpleNamespace(
            id=uuid.uuid4(),
            restaurant_id=RESTAURANT_ID,
            banner_type="hero",
            image_path="banners/hero.jpg",
            sort_order=0,
            is_active=True,
        )
        highlight = SimpleNamespace(
            id=uuid.uuid4(),
            restaurant_id=RESTAURANT_ID,
            banner_type="highlight",
            image_path="banners/destaque.jpg",
            sort_order=1,
            is_active=True,
        )
        service = make_service(
            menu_repository=FakeMenuRepository(banners={"hero": [hero], "highlight": [highlight]})
        )

        menu = service.get_restaurant_menu("pizzaria-do-ze")

        assert [b.image_path for b in menu.banners] == ["banners/hero.jpg"]
        assert [b.image_path for b in menu.highlight_banners] == ["banners/destaque.jpg"]
        assert menu.banners[0].image_url.endswith("banners/hero.jpg")

    def test_no_settings_row_means_settings_is_none(self):
        """`settings=... if settings else None`. Restaurante sem linha de
        configuracao devolve o cardapio assim mesmo, com `settings` nulo — nao
        estoura e nao inventa default."""
        service = make_service(menu_repository=FakeMenuRepository(settings=None))
        assert service.get_restaurant_menu("pizzaria-do-ze").settings is None

    def test_the_settings_row_is_converted(self):
        settings = SimpleNamespace(
            min_order_value=Decimal("20.00"),
            estimated_delivery_time_min=30,
            estimated_delivery_time_max=50,
            default_delivery_fee=Decimal("7.00"),
            service_fee_enabled=False,
            service_fee_amount=Decimal("0.00"),
            accepts_delivery=True,
            accepts_pickup=True,
            payment_methods=["pix"],
            is_open=True,
        )
        service = make_service(menu_repository=FakeMenuRepository(settings=settings))

        response = service.get_restaurant_menu("pizzaria-do-ze").settings

        assert response.min_order_value == 20.00
        assert response.default_delivery_fee == 7.00
        assert response.is_open is True

    def test_a_coupon_takes_its_image_from_the_template(self):
        """O cupom nao tem imagem propria: ela vem de `coupon.template`. Um
        cupom sem template gravado estoura aqui — registrado no teste
        seguinte."""
        coupon = SimpleNamespace(
            id=uuid.uuid4(),
            code="BEMVINDO",
            title="Bem-vindo",
            template=SimpleNamespace(image_path="cupons/bemvindo.png"),
            discount_type="percent",
            discount_value=Decimal("10.00"),
            min_order_value=Decimal("30.00"),
            sort_order=None,
            is_active=True,
        )
        service = make_service(menu_repository=FakeMenuRepository(coupons=[coupon]))

        response = service.get_restaurant_menu("pizzaria-do-ze").coupons[0]

        assert response.name == "Bem-vindo"
        assert response.image_url.endswith("cupons/bemvindo.png")
        # `sort_order or 0`: nulo vira zero em vez de quebrar a ordenacao.
        assert response.sort_order == 0

    def test_a_coupon_without_a_template_raises_attribute_error(self):
        """ESQUISITO, e registrado como esta.

        `_coupon_response` le `coupon.template.image_path` sem conferir se o
        template existe. Um cupom ativo cujo template foi apagado derruba o
        CARDAPIO INTEIRO com AttributeError — nao so aquele cupom — porque a
        montagem e uma comprehension so.

        E a pior forma da falha: o cliente nao ve "cupom indisponivel", ve a
        loja fora do ar. Nao e corrigido aqui.
        """
        coupon = SimpleNamespace(
            id=uuid.uuid4(),
            code="ORFAO",
            title="Orfao",
            template=None,
            discount_type="percent",
            discount_value=Decimal("10.00"),
            min_order_value=Decimal("30.00"),
            sort_order=0,
            is_active=True,
        )
        service = make_service(menu_repository=FakeMenuRepository(coupons=[coupon]))

        with pytest.raises(AttributeError):
            service.get_restaurant_menu("pizzaria-do-ze")
