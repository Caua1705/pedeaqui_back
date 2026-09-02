"""Objetos TRANSIENTES do dominio, para a suite rapida. Sem banco, sem sessao.

Irma de `tests/fabricas_db.py`, e a diferenca e a unica que importa: aquela
recebe `db` e GRAVA; esta nao toca em banco nenhum. `Branch(...)` sem
`db.add()` e um objeto Python comum — a coluna que ninguem passa vale `None`,
o atributo que nao existe levanta `TypeError`, e nada disso precisa de
Postgres. Serve na suite `-m "not db"` inteira.

## Por que ela existe

O CLAUDE.md proibe dublar schema ou model com `SimpleNamespace`, e a razao nao
e purismo: **um objeto de atributos livres nao tem o contrato que o teste diz
estar verificando.** Ele responde qualquer atributo que o teste escrever e
nenhum que o teste esquecer. O caso que fechou a porta foi `serves_people`,
que entrou em `products` e em duas superficies mas nao no `ProductResponse` —
os testes rapidos da voz montavam o produto com `SimpleNamespace`, o atributo
existia PORQUE O TESTE O ESCREVEU, a suite ficou verde, e `buscar_no_cardapio`
levantava `AttributeError` em toda busca falada em producao.

Sem um lugar como este, cada teste que precisa de uma filial escreve a propria
filial de mentira — e foi assim que a suite acumulou 141 dubles de dado.
Escrever `Branch(...)` a mao em 141 lugares tambem resolveria; teria o defeito
de a proxima coluna obrigatoria precisar de 141 edicoes.

## O que estas funcoes NAO dao

**`default=` de coluna.** O `default=True` de `Branch.is_open` e aplicado pelo
SQLAlchemy no INSERT, e aqui nao ha INSERT: `Branch().is_open` e `None`, nao
`True`. Por isso cada funcao aqui escreve explicitamente o que o banco
escreveria — e por isso mudar um default no model NAO muda o que a suite ve,
o que e uma limitacao honesta e nao um bug.

**Consistencia referencial.** `produto()` gera `branch_id` proprio se ninguem
passar um; nao ha FK para reclamar. Teste que dependa de o produto ser DA
filial passa a filial.
"""

import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

from src.models.admin_user_model import AdminUser
from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_model import Branch
from src.models.branch_payment_method_model import BranchPaymentMethod
from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.models.courier_model import Courier, CourierAssignment
from src.models.customer_model import Customer, CustomerAddress
from src.models.order_model import Order
from src.models.product_model import Product
from src.models.product_option_model import ProductOption, ProductOptionGroup
from src.models.restaurant_banner_model import RestaurantBanner
from src.models.restaurant_model import Restaurant
from src.models.restaurant_setting_model import RestaurantSetting
from src.schemas.product_schema import ProductResponse
from src.services.delivery_estimate_service import DeliveryEstimateResult
from src.services.payment_credential_service import ActivePaymentCredential


def restaurante(**sobrescritas) -> Restaurant:
    campos = {
        "id": uuid.uuid4(),
        "name": "Junior da Picanha",
        "slug": "junior-da-picanha",
        "is_active": True,
    }
    campos.update(sobrescritas)
    return Restaurant(**campos)


def filial(**sobrescritas) -> Branch:
    """Uma filial ABERTA que nao sobrescreve nada do restaurante.

    Os nulos de `min_order_value` para baixo nao sao preguica: e o estado em
    que toda filial nasce, e e ele que faz a heranca do restaurante ser
    exercitada. Filial que sobrescreve e o caso especial, e quem o quer passa
    o valor.
    """
    campos = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "name": "Matriz",
        "slug": "matriz",
        "address": "Rua das Flores",
        "neighborhood": "Varjota",
        "city": "Fortaleza",
        "state": "CE",
        "is_open": True,
        "accepts_delivery": True,
        "accepts_pickup": True,
        "delivery_paused_until": None,
        "delivery_pause_reason": None,
        "min_order_value": None,
        "service_fee_enabled": None,
        "service_fee_amount": None,
        "estimated_delivery_time_min": None,
        "estimated_delivery_time_max": None,
        "default_delivery_fee": None,
        "free_delivery_enabled": None,
        "free_delivery_min_order_value": None,
        "is_main": True,
        "is_active": True,
    }
    campos.update(sobrescritas)
    return Branch(**campos)


def configuracoes(**sobrescritas) -> RestaurantSetting:
    """`restaurant_settings` — o padrao que a filial herda quando nao sobrescreve.

    `platform_commission_percent` e escrito porque e `nullable=False` no model
    e a comissao sai dele: deixar nulo aqui faria todo teste de comissao
    comecar por um `TypeError` que nao e o assunto dele.
    """
    campos = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "min_order_value": Decimal("0"),
        "estimated_delivery_time_min": None,
        "estimated_delivery_time_max": None,
        "default_delivery_fee": None,
        "service_fee_enabled": False,
        "service_fee_amount": Decimal("0"),
        "free_delivery_enabled": None,
        "free_delivery_min_order_value": None,
        "platform_commission_percent": Decimal("10.00"),
        "voice_enabled": False,
    }
    campos.update(sobrescritas)
    return RestaurantSetting(**campos)


def produto(**sobrescritas) -> Product:
    """Um produto ativo e disponivel, com `option_groups` vazio.

    `option_groups` e relacionamento e nao coluna: numa instancia transiente
    ele ja nasce lista vazia, e escreve-lo aqui e so para o teste poder
    passar grupos sem saber disso.
    """
    campos = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "branch_id": uuid.uuid4(),
        "category_id": uuid.uuid4(),
        "name": "Picanha",
        "price": Decimal("50.00"),
        "is_active": True,
        "is_available": True,
        "sort_order": 0,
        "option_groups": [],
    }
    campos.update(sobrescritas)
    return Product(**campos)


def cliente(**sobrescritas) -> Customer:
    campos = {
        "id": uuid.uuid4(),
        "name": "Fulano de Tal",
        "email": "fulano@example.com",
        "phone": "85999990000",
        "password_hash": "$2b$12$dublê-que-nao-e-verificado",
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
        "city": "Fortaleza",
        "state": "CE",
        "is_default": True,
    }
    campos.update(sobrescritas)
    return CustomerAddress(**campos)


def usuario_do_painel(**sobrescritas) -> AdminUser:
    campos = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "branch_id": None,
        "name": "Dona do Junior",
        "email": "dona@junior.com",
        "password_hash": "$2b$12$dublê-que-nao-e-verificado",
        "role": "owner",
        "is_active": True,
        "must_change_password": False,
    }
    campos.update(sobrescritas)
    return AdminUser(**campos)


def forma_de_pagamento(**sobrescritas) -> BranchPaymentMethod:
    campos = {
        "id": uuid.uuid4(),
        "branch_id": uuid.uuid4(),
        "method_type": "cash",
        "payment_flow": "delivery",
        "label": "Dinheiro",
        "enabled": True,
        "earns_cashback": True,
        "requires_gateway": False,
        "sort_order": 0,
    }
    campos.update(sobrescritas)
    return BranchPaymentMethod(**campos)


def horario(**sobrescritas) -> BranchBusinessHour:
    """Uma faixa de funcionamento da filial — o que `ensure_branch_is_open` devolve.

    `weekday` e o do PYTHON: 0 = segunda, 6 = domingo (armadilha 1). Faixa que
    vira a noite pertence ao dia em que COMECA.
    """
    campos = {
        "id": uuid.uuid4(),
        "branch_id": uuid.uuid4(),
        "weekday": 0,
        "opens_at": time(11, 0),
        "closes_at": time(23, 0),
        "prep_time_min": 20,
        "prep_time_max": 30,
        "is_closed": False,
        "sort_order": 0,
    }
    campos.update(sobrescritas)
    return BranchBusinessHour(**campos)


def estimativa_de_entrega(**sobrescritas) -> DeliveryEstimateResult:
    """O que `DeliveryEstimateService.estimate` devolve — e nao o que a rota responde.

    Sao dois tipos parecidos e diferentes, e a diferenca morde: o
    `DeliveryEstimateResult` (dataclass) tem `latitude`/`longitude`, e
    `to_response()` os REMOVE ao montar o `DeliveryEstimateResponse`. Quem
    dubla o resultado com o schema da resposta constroi um objeto sem as duas
    coordenadas — e `OrderService` le `delivery_estimate.latitude` ao gravar o
    pedido.

    Por ser dataclass sem default, ela obriga o teste a passar tudo; o que
    esta escrito aqui e a entrega atendida comum.
    """
    campos = {
        "serviceable": True,
        "reason": None,
        "message": None,
        "distance_km": 4.2,
        "travel_time_min": 18,
        "prep_time_min": 20,
        "prep_time_max": 30,
        "eta_min": 38,
        "eta_max": 48,
        "delivery_fee": 8.0,
        "provider": "google_routes",
        "fallback": False,
        "latitude": None,
        "longitude": None,
    }
    campos.update(sobrescritas)
    return DeliveryEstimateResult(**campos)


def banner(**sobrescritas) -> RestaurantBanner:
    campos = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "banner_type": "hero",
        "image_path": "banners/hero.jpg",
        "sort_order": 0,
        "is_active": True,
    }
    campos.update(sobrescritas)
    return RestaurantBanner(**campos)


def arte_de_cupom(**sobrescritas) -> CouponTemplate:
    """A arte da vitrine (`coupon_templates`).

    `image_path` vai escrito porque a coluna e NOT NULL no banco e o model a
    declara opcional — a armadilha que `tests/test_models_nunca_instanciados.py`
    vigia em `src/` e `scripts/`. Aqui, onde instanciar e permitido, o jeito
    certo e este: passar a coluna.
    """
    campos = {
        "id": uuid.uuid4(),
        "name": "Bem-vindo",
        "image_path": "cupons/bemvindo.png",
        "discount_type": "percent",
        "discount_value": Decimal("10.00"),
        "sort_order": 0,
        "is_active": True,
    }
    campos.update(sobrescritas)
    return CouponTemplate(**campos)


def cupom(**sobrescritas) -> RestaurantCoupon:
    """Uma campanha publica, ativa e dentro da janela.

    `template` e relacionamento: passar `template=None` descreve o cupom cuja
    arte foi apagada, que e um caso REAL e testado (o cardapio deixa ele de
    fora em vez de cair).
    """
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
        "sort_order": 0,
        "is_active": True,
    }
    if "template" not in sobrescritas:
        campos["template"] = arte_de_cupom()
    campos.update(sobrescritas)
    return RestaurantCoupon(**campos)


def pedido(**sobrescritas) -> Order:
    """Um pedido pago, de retirada, ja com a comissao calculada.

    `tracking_token_hash` vai escrito e `tracking_token` NAO EXISTE: a coluna
    em texto puro saiu do model, e um dublê que a escrevesse descreveria um
    pedido que a aplicacao nao produz mais. Quem precisa do token em claro
    guarda a string do lado, como o service faz.

    Os `Decimal` sao os do banco, e nao `float`: `Numeric` volta como
    `Decimal`, e teste que compare com `float` compara outro tipo.
    """
    campos = {
        "id": uuid.uuid4(),
        "order_number": 1,
        "tracking_token_hash": "hash-do-token",
        "restaurant_id": uuid.uuid4(),
        "branch_id": uuid.uuid4(),
        "customer_id": None,
        "customer_name_snapshot": "Fulano de Tal",
        "customer_phone_snapshot": "85999990000",
        "order_type": "pickup",
        "status": "pending",
        "payment_method": "pix",
        "payment_status": "paid",
        "refunded_amount": Decimal("0.00"),
        "subtotal": Decimal("100.00"),
        "delivery_fee": Decimal("0.00"),
        "service_fee": Decimal("0.00"),
        "coupon_discount_amount": Decimal("0.00"),
        "cashback_redeemed_amount": Decimal("0.00"),
        "discount_total": Decimal("0.00"),
        "total": Decimal("100.00"),
        "commission_percent": Decimal("10.00"),
        "commission_base_amount": Decimal("100.00"),
        "commission_amount": Decimal("10.00"),
        "delivery_fee_waived": Decimal("0.00"),
        "created_at": datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
    }
    campos.update(sobrescritas)
    return Order(**campos)


def cartao_de_produto(**sobrescritas) -> ProductResponse:
    """O produto COMO A API o entrega — e como a voz e o `/chat` o leem.

    E o schema da armadilha do `serves_people`: ele nasceu so no
    `AdminProductResponse`, `ProductResponse` ficou sem, e `buscar_no_cardapio`
    levantava `AttributeError` em toda busca falada. Dublê solto nao teria
    denunciado nada; este constroi o schema de verdade, entao campo que sair
    daqui quebra na hora.

    `price` e `float` e nao `Decimal`, de proposito: e o que o schema declara,
    e a coluna e `NOT NULL` — o produto que o cliente ve nunca tem preco nulo.
    """
    campos = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "branch_id": uuid.uuid4(),
        "category_id": uuid.uuid4(),
        "name": "Picanha",
        "price": 50.0,
    }
    campos.update(sobrescritas)
    return ProductResponse(**campos)


def grupo_de_opcoes(**sobrescritas) -> ProductOptionGroup:
    campos = {
        "id": uuid.uuid4(),
        "product_id": uuid.uuid4(),
        "name": "Escolha o ponto",
        "min_select": 1,
        "max_select": 1,
        "is_required": True,
        "sort_order": 0,
        "is_active": True,
        "options": [],
    }
    campos.update(sobrescritas)
    return ProductOptionGroup(**campos)


def opcao(**sobrescritas) -> ProductOption:
    campos = {
        "id": uuid.uuid4(),
        "option_group_id": uuid.uuid4(),
        "name": "Ao ponto",
        "additional_price": Decimal("0.00"),
        "sort_order": 0,
        "is_active": True,
    }
    campos.update(sobrescritas)
    return ProductOption(**campos)


def credencial_de_pagamento(**sobrescritas) -> ActivePaymentCredential:
    """A credencial do restaurante no gateway, ja decifrada.

    `environment` vai escrito porque a dataclass o exige — e o dublê solto o
    omitia. O valor importa: `sandbox` NAO oferece cartao, e um teste que
    dublasse a credencial sem ambiente descreveria uma que o service nunca
    recebe.

    NENHUM valor aqui e segredo de verdade. `access_token` e `webhook_secret`
    sao decifrados na hora em producao e nunca logados; aqui sao textos que
    denunciam vazamento se aparecerem numa resposta.
    """
    campos = {
        "environment": "production",
        "public_key": "TEST-public-do-junior",
        "access_token": "token-secretissimo",
        "webhook_secret": "segredo-do-webhook",
    }
    campos.update(sobrescritas)
    return ActivePaymentCredential(**campos)


def entregador(**sobrescritas) -> Courier:
    """Um motoboy ativo, sem acesso gerado, de uma filial qualquer.

    `access_link_hash` e `access_code_hash` nulos sao o estado em que todo
    cadastro nasce — entre criar e gerar o codigo. Teste que precisa do
    acesso gera pelo service, que e quem sabe a forma dos dois hashes.
    """
    campos = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "branch_id": uuid.uuid4(),
        "name": "Zé do Baião",
        "phone": "85999990000",
        "is_active": True,
        "access_link_hash": None,
        "access_code_hash": None,
        "access_generated_at": None,
        "deleted_at": None,
        "created_at": datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    }
    campos.update(sobrescritas)
    return Courier(**campos)


def atribuicao(**sobrescritas) -> CourierAssignment:
    """Uma corrida ABERTA (`unassigned_at` nulo), com a taxa ja congelada."""
    campos = {
        "id": uuid.uuid4(),
        "order_id": uuid.uuid4(),
        "courier_id": uuid.uuid4(),
        "assigned_by_admin_user_id": None,
        "assigned_at": datetime(2026, 9, 1, 19, tzinfo=timezone.utc),
        "unassigned_at": None,
        "unassigned_by_admin_user_id": None,
        "courier_fee_snapshot": Decimal("7.00"),
        "distance_km_snapshot": Decimal("4.20"),
    }
    campos.update(sobrescritas)
    return CourierAssignment(**campos)
