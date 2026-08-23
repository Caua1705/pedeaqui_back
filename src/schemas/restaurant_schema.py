from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from src.schemas.common_schema import BaseResponse


class RestaurantPublicResponse(BaseResponse):
    id: UUID
    name: str
    slug: str
    description: str | None = None
    logo_path: str | None = None
    logo_url: str | None = None
    cover_path: str | None = None
    cover_url: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    is_active: bool | None = True


class RestaurantSettingsResponse(BaseResponse):
    """A operacao de UMA filial, apesar do nome herdado.

    Os campos nao mudaram de nome nem de tipo, mas mudaram de dono: desde a
    revisao 20260818_0025 este bloco descreve a filial que o `branch_id` do
    `/menu` pediu (ou a filial padrao, quando ele nao vem), e nao mais o
    restaurante inteiro. `is_open` aqui e o "fechar agora" DAQUELA loja.

    O nome fica por ser contrato publicado, e renomear schema quebra o painel
    junto (armadilha 16). O `settings_branch_id` da resposta do cardapio diz
    de qual filial este bloco esta falando.

    `payment_methods` NAO esta mais aqui: saiu na revisao 20260820_0027,
    junto com a coluna `restaurant_settings.payment_methods`. Era dado morto
    e podia discordar do que a filial de fato aceita — quem manda e
    `branch_payment_methods`, por filial, em
    `GET /restaurants/{slug}/info?branch_id=...`.
    """

    min_order_value: float
    estimated_delivery_time_min: int | None = None
    estimated_delivery_time_max: int | None = None
    default_delivery_fee: float
    service_fee_enabled: bool | None = True
    service_fee_amount: float
    accepts_delivery: bool | None = True
    accepts_pickup: bool | None = True
    is_open: bool | None = True

    # A entrega esta acontecendo NESTE minuto. `accepts_delivery` continua
    # dizendo se a filial entrega em geral; este desconta a pausa temporaria
    # (chuva, entregador que sumiu). O app precisa dos dois: o primeiro
    # decide se o botao de entrega existe, o segundo se ele esta ativo agora.
    accepts_delivery_now: bool = True
    delivery_paused_until: datetime | None = None
    delivery_pause_reason: str | None = None

    # A campanha de frete gratis JA RESOLVIDA para esta filial. E o que
    # permite ao carrinho mostrar "faltam R$ 12 para frete gratis" — o app
    # compara com o proprio subtotal.
    #
    # Publicar o VALOR, e nao a decisao, e deliberado: quem decide a taxa e o
    # servidor na criacao do pedido, com o subtotal que ele mesmo calculou.
    # Uma rota que recebesse o subtotal do cliente para responder "gratis ou
    # nao" seria preco vindo do cliente por outra porta.
    free_delivery_enabled: bool = False
    free_delivery_min_order_value: float | None = None

    # As faixas de prazo por distancia, se a filial configurou. Servem a tela
    # que ainda NAO tem endereco: com elas o app diz "25 a 80 min conforme o
    # bairro" em vez de um par unico que mente para metade da cidade.
    #
    # `estimated_delivery_time_min/max` acima continua sendo o rotulo que o
    # lojista digitou, e NAO e recalculado a partir daqui: o campo mudaria de
    # significado sem mudar de nome.
    delivery_time_bands: list["DeliveryTimeBandResponse"] = Field(default_factory=list)

    # Os termos de RESGATE do cashback, ja resolvidos para esta filial. E o
    # que permite ao app dizer "faltam R$ 2 para voce poder usar seu
    # cashback" ANTES de o cliente tentar — ver `BranchCashbackTermsResponse`.
    cashback: "BranchCashbackTermsResponse" = Field(
        default_factory=lambda: BranchCashbackTermsResponse()
    )


class BranchCashbackTermsResponse(BaseResponse):
    """O que a FILIAL precisa dizer antes de o cliente tentar resgatar.

    Ate aqui o app nao tinha como explicar por que o cashback nao descontou.
    Saldo abaixo do minimo devolve zero **sem erro** (`amount_to_redeem`), e a
    unica pista era `cashback_redeemed_amount: 0` na resposta do pedido —
    depois de fechado. `docs/cashback.md` registrou isso como pendencia
    quando o saldo passou a ser por restaurante, e apontou o lugar de
    resolver: aqui, ao lado do cardapio, que e quem conhece a filial.

    **Por que nao coube em `by_restaurant[]` de `/customers/me/cashback`.** O
    saldo e do RESTAURANTE; o resgate acontece numa FILIAL, que pode ter regra
    propria — inclusive `enabled = false` com a rede inteira ligada, que e como
    uma loja sai da campanha. Publicar o piso sob uma chave por restaurante
    mostraria um numero que nao vale na loja em que a pessoa esta pedindo.

    **O app junta as duas pontas:** o saldo daquele restaurante sai de
    `by_restaurant[].balance`, os termos saem daqui, e a frase da tela e a
    comparacao dos dois. Nenhum dos dois lados sozinho responde a pergunta.

    **`percent` NAO esta aqui, e a ausencia e deliberada.** Ele e termo de
    quanto o pedido GERA, nao de resgate, e muda por dia da semana: quem o
    resolve para valer e o checkout, com `order.created_at`
    (`resolve_cashback_terms`). Publica-lo na abertura do cardapio criaria a
    segunda resposta para "quanto gera" — e as duas discordariam sempre que a
    meia-noite caisse entre abrir o cardapio e fechar o pedido.
    """

    # Falso cobre os tres casos que caem em `SEM_CASHBACK`: restaurante sem
    # regra, regra desligada e filial que saiu da campanha. Para a tela do
    # cliente os tres sao a mesma frase — "esta loja nao tem cashback" —, e
    # distingui-los aqui publicaria configuracao do lojista sem servir a
    # decisao nenhuma do app.
    enabled: bool = False

    # O piso e do SALDO, e nao do resgate: com R$ 3 acumulados e minimo de
    # R$ 5 nao ha resgate nenhum, nem parcial. A tela que trata isto como
    # "valor minimo a resgatar" oferece um resgate de R$ 3 que o servidor
    # ignora em silencio.
    min_redeem_balance: float = 0.0


class DeliveryTimeBandResponse(BaseResponse):
    """Uma faixa de prazo por distancia.

    `max_distance_km` e um TETO: vale a primeira faixa, em ordem crescente,
    cujo teto alcanca a distancia do endereco. Nao ha piso porque nao ha
    buraco — a faixa anterior cobre tudo abaixo dela.

    Os minutos sao o DESLOCAMENTO, e nao o prazo total: o preparo da filial
    soma por cima, e e o servidor que faz essa conta em
    `POST /delivery/estimate`. Somar aqui, no app, daria dois numeros
    diferentes para o mesmo pedido.
    """

    max_distance_km: float
    delivery_time_min: int
    delivery_time_max: int


class BranchResponse(BaseResponse):
    id: UUID
    name: str
    slug: str
    address: str
    neighborhood: str
    city: str
    state: str
    zipcode: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_main: bool | None = False
    is_active: bool | None = True


class CategoryResponse(BaseResponse):
    """Uma categoria do cardapio de UMA filial.

    `branch_id` entrou na revisao 20260820_0026 e vale como conferencia: o
    `/menu` inteiro fala de uma filial so, entao todas as categorias da
    resposta trazem o mesmo valor. Uma tela que o compare com o `branch_id`
    da raiz percebe na hora que esta misturando duas cargas.
    """

    id: UUID
    branch_id: UUID
    name: str
    slug: str
    sort_order: int | None = 0
    is_active: bool | None = True


class RestaurantInfoRestaurantResponse(BaseModel):
    id: UUID
    name: str
    logo_url: str | None = None


class BranchAddressResponse(BaseModel):
    street: str | None = None
    number: str | None = None
    neighborhood: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    full_address: str


class RestaurantInfoBranchResponse(BaseModel):
    id: UUID
    name: str
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    address: BranchAddressResponse


class BusinessHourPeriodResponse(BaseModel):
    opens_at: str
    closes_at: str


class BusinessHourDayResponse(BaseModel):
    weekday: int
    day_label: str
    periods: list[BusinessHourPeriodResponse]
    is_closed: bool


PaymentFlow = Literal["online", "delivery"]
PaymentMethodType = Literal[
    "pix", "credit_card", "debit_card", "cash", "voucher", "meal_voucher", "other"
]


class BranchPaymentMethodResponse(BaseModel):
    id: UUID
    payment_flow: PaymentFlow
    method_type: PaymentMethodType
    brand: str | None = None
    label: str
    icon_key: str | None = None
    enabled: bool
    requires_gateway: bool


class PaymentMethodsResponse(BaseModel):
    online: list[BranchPaymentMethodResponse]
    delivery: list[BranchPaymentMethodResponse]


class RestaurantInfoResponse(BaseModel):
    restaurant: RestaurantInfoRestaurantResponse
    branch: RestaurantInfoBranchResponse
    business_hours: list[BusinessHourDayResponse]
    payment_methods: PaymentMethodsResponse
    timezone: Literal["America/Fortaleza"] = "America/Fortaleza"
    current_weekday: int
    current_day_label: str
