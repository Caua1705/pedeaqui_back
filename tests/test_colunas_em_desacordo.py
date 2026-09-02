"""O que quebra quando o banco entrega NULL numa coluna que o model jura NOT NULL.

`scripts/divergencias_orm_schema.py` acha 42 colunas em que o `nullable=` do
model e o DDL do banco discordam, e 16 delas sao da classe que MORDE NA
LEITURA: o ORM diz `Mapped[str]`, o banco aceita NULL, e nada no repositorio
confere os dois lados — `Base.metadata.create_all()` nao e usado (armadilha
24), entao a anotacao nunca vira DDL.

O levantamento inteiro, com o que quebra e o que nao quebra, esta em
`scratchpad/rodada-back-2.md`, secao 1.1. Este arquivo e a parte executavel
dele: para cada coluna com risco REAL, um teste que constroi o tipo de verdade
com a coluna nula e mostra o caminho de leitura quebrando.

POR QUE ISTO E TESTE E NAO PARAGRAFO. O alinhamento de verdade e
`ALTER TABLE ... SET NOT NULL`, que e migracao contra producao e nao cabe numa
rodada de leitura. Ate ela existir, o que se pode fazer e deixar o defeito
DESCRITO num lugar que roda: quando o schema for alinhado, estes testes
continuam verdes (o schema de resposta nao muda), mas se alguem afrouxar um
schema de resposta para `| None` "para nao dar 500", o teste vira vermelho e
diz que o buraco foi escondido em vez de fechado.

**Instancia transiente, nunca `SimpleNamespace`** (CLAUDE.md). Um objeto de
atributos livres responderia qualquer coisa que o teste escrevesse, inclusive
um campo que o schema nao tem — e o teste ficaria verde descrevendo um
`RestaurantCoupon` que a aplicacao nao produz. `RestaurantCoupon(...)` sem
sessao e sem banco e um objeto Python comum: coluna que o teste nao passa vale
`None`, e nome errado levanta `TypeError` na hora.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.models.admin_user_model import AdminUser
from src.models.coupon_model import RestaurantCoupon
from src.models.customer_model import Customer, CustomerAddress
from src.models.order_item_model import OrderItem
from src.models.order_item_option_model import OrderItemOption
from src.schemas.admin_user_schema import AdminUserDetailResponse
from src.schemas.coupon_schema import CouponAdminResponse
from src.schemas.customer_schema import CustomerAddressResponse
from src.schemas.order_schema import CreateOrderRequest
from src.services.coupon_service import CouponService
from src.services.customer_service import CustomerService
from src.services.order_service import OrderService
from src.utils.security import verify_password


def cliente(**sobrescritas) -> Customer:
    """Um cliente completo. Cada teste anula UMA coluna e nada mais."""
    campos = {
        "id": uuid.uuid4(),
        "name": "Fulano de Tal",
        "email": "fulano@example.com",
        "phone": "85999990000",
        "password_hash": "$2b$12$naoimporta",
        "birth_date": date(1990, 5, 17),
        "marketing_opt_in": False,
        "is_active": True,
    }
    campos.update(sobrescritas)
    return Customer(**campos)


def endereco(**sobrescritas) -> CustomerAddress:
    campos = {
        "id": uuid.uuid4(),
        "customer_id": uuid.uuid4(),
        "street": "Rua das Flores",
        "number": "200",
        "neighborhood": "Varjota",
        "is_default": True,
    }
    campos.update(sobrescritas)
    return CustomerAddress(**campos)


def cupom(**sobrescritas) -> RestaurantCoupon:
    campos = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "coupon_template_id": uuid.uuid4(),
        "code": "BEMVINDO",
        "title": "Bem-vindo",
        "discount_type": "percent",
        "discount_value": Decimal("10.00"),
        "min_order_value": Decimal("0.00"),
        "valid_from": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "valid_until": datetime(2026, 12, 31, tzinfo=timezone.utc),
        "first_order_only": False,
        "visibility": "public",
        "is_active": True,
        "sort_order": 0,
    }
    campos.update(sobrescritas)
    return RestaurantCoupon(**campos)


def usuario_do_painel(**sobrescritas) -> AdminUser:
    campos = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "branch_id": None,
        "name": "Dona do Junior",
        "email": "dona@junior.com",
        "password_hash": "$2b$12$naoimporta",
        "role": "owner",
        "is_active": True,
        "must_change_password": False,
    }
    campos.update(sobrescritas)
    return AdminUser(**campos)


# ── A pior das 16: `restaurant_coupons.valid_until` ─────────────────────────
#
# Ela e a unica que quebra em Python ANTES de chegar ao schema de resposta, e o
# lugar onde quebra e o checkout.


def test_cupom_sem_valid_until_derruba_a_avaliacao_do_checkout():
    """`CouponService._aware(None)` levanta — e e ele que abre `evaluate`.

    `evaluate` e alcancada por `preview` e por `lock_and_validate_for_order`,
    e as duas chegam ao cupom por `_find_coupon` (id ou codigo), que NAO
    filtra a janela de validade. Ou seja: a consulta que protege a vitrine
    (`list_in_window`, com `valid_until >= now`) nao protege o checkout —
    linha nula nunca casa naquele WHERE, mas chega inteira aqui.
    """
    sem_validade = cupom(valid_until=None)

    with pytest.raises(AttributeError):
        CouponService._aware(sem_validade.valid_until)


def test_cupom_sem_valid_until_derruba_a_lista_do_painel():
    """`list_admin` monta `CouponAdminResponse.model_validate(coupon)`.

    O campo e `valid_until: datetime`, sem `| None`: a linha nula vira 500 na
    tela de campanhas — e derruba a LISTA inteira, nao so aquele cartao.
    """
    with pytest.raises(ValidationError):
        CouponAdminResponse.model_validate(cupom(valid_until=None))


# ── O cliente: `customers.email` e `customers.birth_date` ───────────────────


@pytest.mark.parametrize("coluna_nula", ["email", "birth_date"])
def test_cliente_com_coluna_nula_derruba_o_proprio_perfil(coluna_nula):
    """`GET /customers/me` e a exportacao da LGPD passam pelo mesmo schema.

    `CurrentCustomerResponse` declara `email: str` e `birth_date: date`, os
    dois sem `| None`. O cliente que tivesse qualquer um dos dois nulo nao
    conseguiria abrir o proprio perfil — nem exportar os proprios dados, que
    e direito e nao comodidade.

    `CustomerService(None)`: `get_me` nao toca no banco, so traduz o objeto
    que ja recebeu. Os repositorios do `__init__` apenas guardam a sessao.
    """
    servico = CustomerService(None)

    with pytest.raises(ValidationError):
        servico.get_me(cliente(**{coluna_nula: None}))


def test_cliente_sem_password_hash_nao_quebra_nada():
    """A excecao das 16, e ela e por escrito.

    `verify_password` declara `password_hash: str | None` e devolve `False`
    quando nao ha hash. Nulo aqui e falha FECHADA — a pessoa nao entra, e
    nenhuma rota estoura. E o unico dos 16 casos em que a defesa ja existe.
    """
    assert verify_password("qualquer senha", cliente(password_hash=None).password_hash) is False


# ── O endereco: `customer_addresses.number` e `.neighborhood` ───────────────


@pytest.mark.parametrize("coluna_nula", ["number", "neighborhood"])
def test_endereco_com_coluna_nula_derruba_a_lista_de_enderecos(coluna_nula):
    """`GET /customers/me/addresses` responde `list[CustomerAddressResponse]`.

    Como e a LISTA que o FastAPI valida, um endereco velho com numero nulo
    leva junto todos os outros enderecos daquele cliente — inclusive os que
    estao inteiros. O mesmo schema e usado por `export_me`.
    """
    with pytest.raises(ValidationError):
        CustomerAddressResponse.model_validate(endereco(**{coluna_nula: None}))


def test_endereco_sem_numero_nao_engana_a_validacao_do_pedido():
    """O checkout, ao contrario, ja trata nulo — e vale registrar por que.

    `_validate_delivery_address` escreve `not value or not value.strip()`, e
    `not None` e verdadeiro: o ramo do 400 acontece antes de qualquer
    `.strip()`. Se um dia isso virar so `not value.strip()`, este teste vira
    vermelho e diz o que se perdeu.
    """
    payload = CreateOrderRequest.model_construct(order_type="delivery")

    with pytest.raises(HTTPException) as erro:
        OrderService._validate_delivery_address(None, payload, endereco(number=None))

    assert erro.value.status_code == 400


# ── A comanda: `order_item_options.option_group_id` e `.option_id` ──────────


@pytest.mark.parametrize("coluna_nula", ["option_group_id", "option_id"])
def test_adicional_com_coluna_nula_derruba_o_detalhe_do_pedido(coluna_nula):
    """`_item_option_groups` monta os dois schemas que declaram `UUID` puro.

    O caminho e o mesmo do detalhe do pedido para o cliente, da comanda que
    vai para a impressora e da tela do painel: os tres passam por aqui. Um
    unico adicional com a coluna nula derruba o PEDIDO inteiro, nao so o
    adicional.

    O banco hoje nao produz esse nulo por conta propria — as duas FKs sao
    `NO ACTION`, entao apagar o grupo de opcoes e recusado em vez de anular a
    linha historica. O risco e de linha escrita fora do ORM.
    """
    campos = {
        "id": uuid.uuid4(),
        "order_item_id": uuid.uuid4(),
        "option_group_id": uuid.uuid4(),
        "option_id": uuid.uuid4(),
        "option_group_name_snapshot": "Acompanhamento",
        "option_name_snapshot": "Espaguete",
        "additional_price_snapshot": Decimal("0.00"),
    }
    campos[coluna_nula] = None

    item = OrderItem(
        id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        product_name_snapshot="Picanha",
        unit_price_snapshot=Decimal("89.90"),
        quantity=1,
        total=Decimal("89.90"),
    )
    item.options.append(OrderItemOption(**campos))

    with pytest.raises(ValidationError):
        OrderService._item_option_groups(item)


# ── O painel: `admin_users.is_active` ──────────────────────────────────────


def test_lojista_com_is_active_nulo_derruba_a_lista_de_usuarios():
    """`AdminUserDetailResponse.is_active: bool`, sem `| None`.

    Esta coluna tem `DEFAULT true` no banco, entao so fica nula por INSERT que
    escreva NULL de proposito — e por isso ela e a menos provavel das oito.
    Continua sendo a que derruba `GET /admin/users` inteiro quando acontece.
    """
    with pytest.raises(ValidationError):
        AdminUserDetailResponse.model_validate(usuario_do_painel(is_active=None))


def test_lojista_com_is_active_nulo_nao_consegue_entrar():
    """No login o nulo NAO quebra: `if not admin_user.is_active` e falsy.

    Falha fechada, que e o lado certo de errar — mas o sintoma que chega e
    "senha ou usuario invalido" para quem tem os dois certos. Fica registrado
    para nao se procurar bug de senha quando o problema e o schema.
    """
    assert not usuario_do_painel(is_active=None).is_active
