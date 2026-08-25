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

from src.models.product_model import Product
from src.models.product_option_model import ProductOption, ProductOptionGroup
from src.services.menu_service import MenuService


RESTAURANT_ID = uuid.uuid4()
# O cardapio e da FILIAL desde a revisao 20260820_0026. Os fakes deste arquivo
# tem uma filial so, entao todo produto e toda categoria nascem nela — o que
# este arquivo mede (ordem e filtro dos adicionais) nao depende de qual e.
BRANCH_ID = uuid.uuid4()


class FakeDb:
    """O MenuService so guarda a sessao para entregar aos repositorios, que
    sao trocados por fakes logo em seguida. Nada aqui e chamado."""


class FakeMenuRepository:
    def __init__(self, settings=None, branches=(), banners=None, coupons=(), categories=(), products=()):
        self.settings = settings
        self.branches = list(branches)
        self.banners = banners or {}
        self.categorias_pedidas_para = None
        self.produtos_pedidos_para = None
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

    def get_active_categories(self, branch_id):
        self.categorias_pedidas_para = branch_id
        return self.categories

    def get_active_products(self, branch_id):
        self.produtos_pedidos_para = branch_id
        return self.products


def make_branch(is_open=True, **sobrescritas):
    """Uma filial. Por default nao sobrescreve nada e herda o restaurante.

    O bloco `settings` do cardapio descreve UMA filial desde a revisao
    20260818_0025 — por isso o MenuService precisa de uma, e por isso um
    valor passado aqui vence o do restaurante.
    """
    campos = {
        "min_order_value": None,
        "service_fee_enabled": None,
        "service_fee_amount": None,
        "estimated_delivery_time_min": None,
        "estimated_delivery_time_max": None,
        "default_delivery_fee": None,
        "free_delivery_enabled": None,
        "free_delivery_min_order_value": None,
    }
    campos.update(sobrescritas)
    return SimpleNamespace(
        id=BRANCH_ID,
        restaurant_id=RESTAURANT_ID,
        is_open=is_open,
        accepts_delivery=True,
        accepts_pickup=True,
        delivery_paused_until=None,
        delivery_pause_reason=None,
        **campos,
    )


class FakeBranchRepository:
    def __init__(self, default_branch=None, branches=(), delivery_time_bands=()):
        self.default_branch = default_branch
        self.branches = list(branches)
        self.delivery_time_bands = list(delivery_time_bands)

    def get_default_branch(self, restaurant_id):
        return self.default_branch

    def get_active_by_id_and_restaurant(self, branch_id, restaurant_id):
        return next((b for b in self.branches if b.id == branch_id), None)

    def list_delivery_time_bands(self, branch_id):
        return list(self.delivery_time_bands)


class FakeCashbackRuleRepository:
    """Sem regra nenhuma, que e o estado em que todo restaurante nasce.

    Quem mede a heranca de verdade e
    `tests/test_termos_de_cashback_no_cardapio_db.py`, contra o Postgres:
    qual das duas linhas de `cashback_rules` vale e CONSULTA, e dubla-la aqui
    seria testar o duble.
    """

    def __init__(self, da_filial=None, do_restaurante=None):
        self.da_filial = da_filial
        self.do_restaurante = do_restaurante
        self.filial_pedida = None

    def get_rules_for_branch(self, restaurant_id, branch_id):
        self.filial_pedida = branch_id
        return self.da_filial, self.do_restaurante


class FakeProductRepository:
    def __init__(self, category_exists=True, by_category=(), by_slug=None):
        self.category_exists = category_exists
        self.by_category = list(by_category)
        self.by_slug = by_slug
        self.filial_pedida = None

    def active_category_exists(self, branch_id, category_slug):
        return self.category_exists

    def list_active_by_category_slug(self, branch_id, category_slug):
        return self.by_category

    def get_active_by_slug(self, branch_id, product_slug):
        self.filial_pedida = branch_id
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


# OS TRES FIXTURES ABAIXO USAM O MODEL DE VERDADE, e nao `SimpleNamespace`.
#
# Eles alimentam `product_response`, que le COLUNA POR COLUNA. Com um dublê
# solto, coluna nova que entre no model e no `product_response` — e que nao
# esteja escrita aqui — vira `AttributeError`; e coluna que exista aqui com o
# nome errado nunca e denunciada. Foi exatamente o que aconteceu com
# `serves_people` (revisao 20260825_0039) na ponta da voz.
#
# Uma instancia TRANSIENTE do model resolve os dois lados de graca: coluna nao
# passada vale `None` (nao estoura), e kwarg que nao seja coluna levanta
# `TypeError` na hora. Nao ha sessao, nao ha banco, e estes testes continuam
# fora do marcador `db`.
#
# O que NAO se ganha, e vale saber: `default=` de coluna so e aplicado no
# INSERT, entao aqui ele nao vale — o que estes fixtures escreverem e o que a
# funcao le. Por isso os campos continuam explicitos.


def make_option(name, sort_order=0, is_active=True, additional_price="2.50"):
    return ProductOption(
        id=uuid.uuid4(),
        name=name,
        description=None,
        additional_price=Decimal(additional_price),
        sort_order=sort_order,
        is_active=is_active,
    )


def make_group(name, options=None, sort_order=0, is_active=True):
    """Um grupo de adicionais VALIDO por padrao — com uma opcao ativa dentro.

    O default era `()`, e os testes de ordenacao aproveitavam isso para nao
    escrever opcao nenhuma. Depois que "grupo sem opcao ativa nao existe para
    o cliente" virou regra, um grupo vazio deixou de sair no cardapio e
    aqueles testes passaram a medir a remocao em vez da ordem.

    A ASSERCAO DELES NAO MUDOU — o que mudou foi o fixture, que agora entrega
    um grupo que o cliente consegue responder. Quem precisa de um grupo vazio
    passa as opcoes inativas explicitamente.
    """
    if options is None:
        options = [make_option("padrao")]
    group = ProductOptionGroup(
        id=uuid.uuid4(),
        name=name,
        description=None,
        min_select=0,
        max_select=1,
        is_required=False,
        sort_order=sort_order,
        is_active=is_active,
    )
    group.options = list(options)
    return group


def make_product(name="X-Burger", slug="x-burger", price="24.90", image_path=None, option_groups=()):
    product = Product(
        id=uuid.uuid4(),
        restaurant_id=RESTAURANT_ID,
        branch_id=BRANCH_ID,
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
    )
    product.option_groups = list(option_groups)
    return product


def make_service(
    menu_repository=None,
    product_repository=None,
    restaurant_service=None,
    branch_repository=None,
    cashback_rule_repository=None,
):
    service = MenuService(FakeDb())
    service.menu_repository = menu_repository or FakeMenuRepository()
    service.branch_repository = branch_repository or FakeBranchRepository(make_branch())
    service.cashback_rule_repository = cashback_rule_repository or FakeCashbackRuleRepository()
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

    def test_an_empty_optional_group_disappears_from_the_product(self):
        """MUDANCA DE COMPORTAMENTO INTENCIONAL — nao e refatoracao.

        ANTES: o filtro de grupo e o de opcao eram independentes, entao um
        grupo ATIVO cujas opcoes estavam todas inativas continuava saindo no
        cardapio com a lista vazia. Este teste afirmava `len(groups) == 1` e
        `groups[0].options == []`.

        DEPOIS: grupo sem opcao ativa nao oferece escolha nenhuma, entao nao
        aparece.

        Isto vale para o grupo OPCIONAL, e e cosmetico. Quando o grupo vazio e
        OBRIGATORIO o produto inteiro sai de venda, e isso esta em
        `TestProductsOutOfSale` — sao decisoes diferentes com pesos
        diferentes.
        """
        opcional = make_group("Bordas", [make_option("x", is_active=False)])
        opcional.is_required = False
        product = make_product(option_groups=[opcional])

        assert MenuService.product_response(product).option_groups == []

    def test_a_group_that_still_has_one_active_option_stays(self):
        """A fronteira: basta UMA opcao ativa para o grupo continuar de pe."""
        product = make_product(
            option_groups=[
                make_group("Quase vazio", [make_option("viva"), make_option("morta", is_active=False)])
            ]
        )

        groups = MenuService.product_response(product).option_groups

        assert [group.name for group in groups] == ["Quase vazio"]
        assert [option.name for option in groups[0].options] == ["viva"]

    def test_an_inactive_group_disappears_whatever_its_options(self):
        product = make_product(
            option_groups=[make_group("Desligado", [make_option("viva")], is_active=False)]
        )

        assert MenuService.product_response(product).option_groups == []


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

    def test_o_serve_n_pessoas_atravessa_para_a_resposta_publica(self):
        """A coluna existe desde a revisao 20260825_0039 e ficou meses so no
        `AdminProductResponse`: o lojista preenchia pelo painel e ninguem do
        lado do cliente lia.

        Quem le e o atendente de VOZ, que monta a linha do produto a partir
        desta resposta (a hidratacao da voz e do `/chat` passa por aqui). Sem
        esta linha, `_serve_quantas_pessoas` levantava `AttributeError` em
        toda busca falada — sem que nenhum teste rapido notasse."""
        product = make_product()
        product.serves_people = 2

        assert MenuService.product_response(product).serves_people == 2

    def test_o_serve_nulo_atravessa_como_nulo_e_nao_como_um(self):
        """NULO e "o lojista nao disse". Carimbar 1 aqui seria o backend
        inventando o fato que a coluna existe para parar de inventar — e
        enquanto o cadastro estiver vazio, e o caso de TODO produto."""
        assert MenuService.product_response(make_product()).serves_people is None


# ---------------------------------------------------------------------------
# Produto fora de venda por grupo obrigatorio vazio
# ---------------------------------------------------------------------------


def make_required_group(name="Escolha o ponto", options=()):
    group = make_group(name, list(options))
    group.is_required = True
    return group


def make_blocked_product(name="Picanha"):
    """Produto com um grupo obrigatorio cuja ultima opcao foi desativada.

    E o caso real: o lojista esgota o ultimo ponto de carne e nao percebe que
    tirou a picanha de venda.
    """
    return make_product(
        name=name,
        slug=name.lower(),
        option_groups=[make_required_group(options=[make_option("mal passado", is_active=False)])],
    )


class TestProductsOutOfSale:
    """GRUPO OBRIGATORIO EXISTE PORQUE A COZINHA NAO PRODUZ SEM AQUELE DADO.

    Sem nenhuma opcao ativa nele, o produto nao tem como ser vendido — vender
    sem a escolha mandaria uma picanha sem ponto para a chapa, e esconderia o
    erro do lojista, porque os pedidos continuariam entrando.
    """

    def test_a_blocked_product_is_not_in_the_menu(self):
        service = make_service(
            menu_repository=FakeMenuRepository(products=[make_blocked_product(), make_product(name="Coca")])
        )

        menu = service.get_restaurant_menu("pizzaria-do-ze")

        assert [produto.name for produto in menu.products] == ["Coca"]

    def test_a_blocked_product_is_not_in_its_category(self):
        service = make_service(
            product_repository=FakeProductRepository(
                by_category=[make_blocked_product(), make_product(name="Coca")]
            )
        )

        produtos = service.get_products_by_category("pizzaria-do-ze", "carnes")

        assert [produto.name for produto in produtos] == ["Coca"]

    def test_the_direct_link_to_a_blocked_product_is_404(self):
        """404 igual a produto inexistente, e nao uma mensagem propria: o link
        do produto e publico e compartilhavel, e distinguir os dois casos
        contaria a quem tem o link o que esta acontecendo dentro da loja."""
        service = make_service(product_repository=FakeProductRepository(by_slug=make_blocked_product()))

        with pytest.raises(HTTPException) as exc:
            service.get_product_detail("pizzaria-do-ze", "picanha")

        assert exc.value.status_code == 404

    def test_one_active_option_is_enough_to_keep_it_selling(self):
        """A fronteira. O lojista reativa uma opcao e o produto volta."""
        produto = make_product(
            name="Picanha",
            option_groups=[
                make_required_group(options=[make_option("mal passado", is_active=False), make_option("ao ponto")])
            ],
        )
        service = make_service(menu_repository=FakeMenuRepository(products=[produto]))

        assert [p.name for p in service.get_restaurant_menu("pizzaria-do-ze").products] == ["Picanha"]

    def test_an_empty_optional_group_does_not_take_the_product_out_of_sale(self):
        """So o OBRIGATORIO tira o produto de venda. Uma pizza sem borda
        recheada disponivel continua sendo uma pizza vendavel."""
        opcional = make_group("Bordas", [make_option("catupiry", is_active=False)])
        opcional.is_required = False
        produto = make_product(name="Pizza", option_groups=[opcional])
        service = make_service(menu_repository=FakeMenuRepository(products=[produto]))

        assert [p.name for p in service.get_restaurant_menu("pizzaria-do-ze").products] == ["Pizza"]

    def test_an_inactive_required_group_does_not_block_either(self):
        """Grupo obrigatorio DESATIVADO nao exige nada — nem no cardapio nem
        no pedido —, entao nao tira o produto de venda."""
        grupo = make_required_group(options=[make_option("x", is_active=False)])
        grupo.is_active = False
        produto = make_product(name="Picanha", option_groups=[grupo])
        service = make_service(menu_repository=FakeMenuRepository(products=[produto]))

        assert [p.name for p in service.get_restaurant_menu("pizzaria-do-ze").products] == ["Picanha"]

    def test_it_names_the_product_and_the_group_in_the_log(self, caplog):
        """O log e metade do "nao perder a venda em silencio" — a outra metade
        e o sinal no /admin. Sem os dois, o produto some da loja e ninguem
        tem onde procurar o porque."""
        service = make_service(menu_repository=FakeMenuRepository(products=[make_blocked_product()]))

        with caplog.at_level("WARNING"):
            service.get_restaurant_menu("pizzaria-do-ze")

        avisos = [registro.getMessage() for registro in caplog.records]
        assert len(avisos) == 1
        assert "Picanha" in avisos[0]
        assert "Escolha o ponto" in avisos[0]


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

    def test_no_active_branch_means_settings_is_none(self):
        """Sem filial nao ha operacao para descrever.

        O bloco deixou de ser do restaurante, entao quem responde por ele e a
        filial. Restaurante sem filial ativa continua devolvendo o cardapio,
        com `settings` nulo — nao estoura e nao inventa default."""
        service = make_service(branch_repository=FakeBranchRepository(None))
        menu = service.get_restaurant_menu("pizzaria-do-ze")
        assert menu.settings is None
        assert menu.settings_branch_id is None

    def test_no_settings_row_still_describes_the_branch(self):
        """Restaurante sem linha de configuracao NAO zera o bloco.

        Zera-lo esconderia o `is_open` da filial, que e NOT NULL e pode estar
        pausada — o app mostraria como aberta uma loja que esta fechada. O que
        falta ali e so o que se herdaria, e disso o default da plataforma da
        conta."""
        filial = make_branch(is_open=False)
        service = make_service(
            menu_repository=FakeMenuRepository(settings=None),
            branch_repository=FakeBranchRepository(filial),
        )

        response = service.get_restaurant_menu("pizzaria-do-ze").settings

        assert response is not None
        assert response.is_open is False
        assert response.min_order_value == 0.0

    def test_the_settings_row_is_converted(self):
        settings = SimpleNamespace(
            min_order_value=Decimal("20.00"),
            estimated_delivery_time_min=30,
            estimated_delivery_time_max=50,
            default_delivery_fee=Decimal("7.00"),
            free_delivery_enabled=None,
            free_delivery_min_order_value=None,
            service_fee_enabled=False,
            service_fee_amount=Decimal("0.00"),
        )
        filial = make_branch()
        service = make_service(
            menu_repository=FakeMenuRepository(settings=settings),
            branch_repository=FakeBranchRepository(filial),
        )

        menu = service.get_restaurant_menu("pizzaria-do-ze")
        response = menu.settings

        assert response.min_order_value == 20.00
        assert response.default_delivery_fee == 7.00
        assert response.is_open is True
        assert menu.branch_id == filial.id
        assert menu.settings_branch_id == filial.id

    def test_the_branch_overrides_the_restaurant(self):
        """O motivo do regime de heranca, num teste.

        A filial do shopping cobra o dobro de minimo e nao cobra taxa de
        servico; o resto continua vindo do restaurante, e mudar o padrao
        continua chegando nela.
        """
        settings = SimpleNamespace(
            min_order_value=Decimal("20.00"),
            estimated_delivery_time_min=30,
            estimated_delivery_time_max=50,
            default_delivery_fee=Decimal("7.00"),
            free_delivery_enabled=None,
            free_delivery_min_order_value=None,
            service_fee_enabled=True,
            service_fee_amount=Decimal("0.99"),
            payment_methods=["pix"],
        )
        filial = make_branch(
            min_order_value=Decimal("40.00"),
            service_fee_enabled=False,
        )
        service = make_service(
            menu_repository=FakeMenuRepository(settings=settings),
            branch_repository=FakeBranchRepository(filial),
        )

        response = service.get_restaurant_menu("pizzaria-do-ze").settings

        assert response.min_order_value == 40.00
        assert response.service_fee_enabled is False
        # Herdados: a filial nao os sobrescreveu.
        assert response.service_fee_amount == 0.99
        assert response.estimated_delivery_time_min == 30
        assert response.default_delivery_fee == 7.00

    def test_a_branch_of_another_restaurant_is_404(self):
        """E nao a filial padrao calada: o app que mandou o id errado
        mostraria o valor minimo de outra loja achando que acertou."""
        service = make_service(branch_repository=FakeBranchRepository(make_branch()))

        with pytest.raises(HTTPException) as raised:
            service.get_restaurant_menu("pizzaria-do-ze", uuid.uuid4())

        assert raised.value.status_code == 404

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

    def test_a_coupon_without_a_template_leaves_the_menu_standing(self):
        """MUDANCA DE COMPORTAMENTO INTENCIONAL — nao e refatoracao.

        ANTES: `_coupon_response` lia `coupon.template.image_path` sem
        conferir o template, e a montagem da lista era uma comprehension so.
        Um cupom ativo cujo template foi apagado levantava AttributeError e
        derrubava o CARDAPIO INTEIRO — o cliente nao via "cupom
        indisponivel", via a loja fora do ar. Este teste afirmava
        `pytest.raises(AttributeError)`.

        DEPOIS: o cupom quebrado sai da lista e o resto da vitrine continua
        de pe. A ausencia do template e registrada no log, porque e defeito
        de dado e alguem precisa ter onde procurar.
        """
        orfao = SimpleNamespace(
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
        bom = SimpleNamespace(
            id=uuid.uuid4(),
            code="BEMVINDO",
            title="Bem-vindo",
            template=SimpleNamespace(image_path="cupons/bemvindo.png"),
            discount_type="percent",
            discount_value=Decimal("10.00"),
            min_order_value=Decimal("30.00"),
            sort_order=0,
            is_active=True,
        )
        service = make_service(menu_repository=FakeMenuRepository(coupons=[orfao, bom]))

        menu = service.get_restaurant_menu("pizzaria-do-ze")

        # O cupom bom sobrevive; o quebrado some sem levar nada junto.
        assert [cupom.code for cupom in menu.coupons] == ["BEMVINDO"]
        assert menu.products is not None

    def test_a_menu_whose_only_coupon_is_broken_is_still_served(self):
        """O caso extremo do teste acima: sem cupom nenhum sobrando, a vitrine
        continua respondendo — com a lista de cupons vazia."""
        orfao = SimpleNamespace(
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
        service = make_service(menu_repository=FakeMenuRepository(coupons=[orfao]))

        assert service.get_restaurant_menu("pizzaria-do-ze").coupons == []


# ---------------------------------------------------------------------------
# O cardapio passou a ser da FILIAL (revisao 20260820_0026)
# ---------------------------------------------------------------------------


class TestOCardapioSaiDaFilial:
    """A pergunta que estas asercoes fazem e sempre a mesma: **de qual loja?**

    Enquanto o cardapio foi do restaurante, `get_active_products` recebia o
    `restaurant_id` e nao havia resposta errada possivel. Agora ha uma por
    chamada, e o custo dela e o cliente vendo — com preco — um produto que a
    loja escolhida nao vende.
    """

    def test_os_produtos_sao_pedidos_para_a_filial_e_nao_para_o_restaurante(self):
        filial = make_branch()
        repositorio = FakeMenuRepository(products=[make_product()])
        service = make_service(
            menu_repository=repositorio,
            branch_repository=FakeBranchRepository(filial),
        )

        service.get_restaurant_menu("pizzaria-do-ze")

        assert repositorio.produtos_pedidos_para == filial.id
        assert repositorio.categorias_pedidas_para == filial.id
        assert filial.id != RESTAURANT_ID

    def test_o_branch_id_da_querystring_manda_no_cardapio_inteiro(self):
        escolhida = make_branch()
        padrao = make_branch()
        padrao.id = uuid.uuid4()
        repositorio = FakeMenuRepository(products=[make_product()])
        service = make_service(
            menu_repository=repositorio,
            branch_repository=FakeBranchRepository(padrao, branches=[padrao, escolhida]),
        )

        menu = service.get_restaurant_menu("pizzaria-do-ze", escolhida.id)

        assert repositorio.produtos_pedidos_para == escolhida.id
        assert menu.branch_id == escolhida.id

    def test_os_termos_de_cashback_sao_pedidos_para_a_MESMA_filial(self):
        """O piso do resgate e da filial, e a filial e a do cardapio.

        Publicar o piso da loja errada e o defeito que este bloco existe para
        nao ter: a tela diria "faltam R$ 2" com base numa regra que nao vale
        onde a pessoa esta pedindo.
        """
        escolhida = make_branch()
        padrao = make_branch()
        padrao.id = uuid.uuid4()
        regras = FakeCashbackRuleRepository()
        service = make_service(
            branch_repository=FakeBranchRepository(padrao, branches=[padrao, escolhida]),
            cashback_rule_repository=regras,
        )

        menu = service.get_restaurant_menu("pizzaria-do-ze", escolhida.id)

        assert regras.filial_pedida == escolhida.id
        assert menu.settings.cashback.enabled is False

    def test_restaurante_sem_filial_ativa_responde_a_vitrine_sem_cardapio(self):
        """200 com listas vazias, e nao 404.

        Nome, banner e cupom sao do restaurante e continuam existindo. O que
        nao existe e loja de onde tirar produto — e devolver o cardapio de
        "nenhuma filial" seria inventar um.
        """
        repositorio = FakeMenuRepository(products=[make_product()], categories=[object()])
        service = make_service(
            menu_repository=repositorio,
            branch_repository=FakeBranchRepository(None),
        )

        menu = service.get_restaurant_menu("pizzaria-do-ze")

        assert menu.products == []
        assert menu.categories == []
        assert menu.settings is None
        assert menu.branch_id is None
        # E o repositorio nem chegou a ser consultado: sem filial nao ha o que
        # perguntar.
        assert repositorio.produtos_pedidos_para is None

    def test_a_rota_de_um_produto_recusa_restaurante_sem_filial(self):
        """404, ao contrario do cardapio inteiro, e a diferenca e de contrato.

        O cardapio tem resposta parcial para dar (a vitrine). "Um produto" nao
        tem: sem loja aquele produto nao existe, e nao ha preco a mostrar.
        """
        service = make_service(branch_repository=FakeBranchRepository(None))

        with pytest.raises(HTTPException) as erro:
            service.get_product_detail("pizzaria-do-ze", "picanha")

        assert erro.value.status_code == 404

    def test_o_produto_e_procurado_dentro_da_filial(self):
        """O slug e unico por `(branch_id, slug)`: sem a filial no WHERE, a
        consulta volta a poder casar mais de uma linha."""
        filial = make_branch()
        repositorio = FakeProductRepository(by_slug=make_product())
        service = make_service(
            product_repository=repositorio,
            branch_repository=FakeBranchRepository(filial),
        )

        service.get_product_detail("pizzaria-do-ze", "x-burger")

        assert repositorio.filial_pedida == filial.id
