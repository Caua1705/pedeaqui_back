"""Contrato das rotas de configuracao do painel (BLOCO C da Fase 3).

`platform_commission_percent` NAO aparece em nenhum schema deste arquivo,
nem para leitura. E o percentual que a plataforma cobra do restaurante,
negociado por contrato: publicar no OpenAPI do painel do lojista o
transformaria em campo de tela, e edita-lo seria o lojista escolhendo
quanto nos paga. Quem precisa dele e o relatorio de comissao
(admin_report_schema), que so mostra o valor ja congelado em cada pedido.
"""

from datetime import time
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from src.schemas.common_schema import BaseResponse
from src.schemas.restaurant_schema import PaymentFlow, PaymentMethodType


MAX_NAME_LENGTH = 120
# Teto de faixas por dia. Almoco e jantar sao duas; quatro cobre qualquer
# operacao real e impede um corpo com mil linhas para o mesmo dia.
MAX_PERIODS_PER_WEEKDAY = 4
# Teto do tempo de preparo, o mesmo que BusinessHourInput ja aceitava.
MAX_PREP_TIME_MINUTES = 600
# Teto de um ajuste rapido. O atalho e para o aperto do dia (+5, -10); quem
# precisa somar duas horas ao prazo esta reconfigurando a semana, e isso e
# PUT /business-hours.
MAX_PREP_TIME_DELTA_MINUTES = 120


class AdminRestaurantSettingsResponse(BaseResponse):
    """Os PADROES do restaurante — o que a filial herda quando nao diverge.

    Nenhum destes valores e o que um pedido usa: o pedido le a filial, e a
    filial cai aqui so nos campos que ela deixou nulos. Quem quer o valor
    efetivo pede `GET /admin/branches/operation`.

    `is_open`, `accepts_delivery` e `accepts_pickup` SAIRAM deste schema na
    revisao 20260818_0025. Eles nao tem padrao: sao o estado do dia de UMA
    loja, e viraram `PATCH /admin/branches/{branch_id}/store-status` e
    `PATCH /admin/branches/{branch_id}/order-types`.
    """

    min_order_value: float
    estimated_delivery_time_min: int | None = None
    estimated_delivery_time_max: int | None = None
    default_delivery_fee: float
    service_fee_enabled: bool | None = True
    service_fee_amount: float


class AdminRestaurantSettingsUpdate(BaseModel):
    """Edicao parcial das configuracoes do restaurante (BLOCO C5).

    `default_delivery_fee` NAO e a taxa de entrega do dia a dia — essa sai da
    regra por km da filial (`delivery_base_fee` + `delivery_fee_per_km`, em
    PATCH /admin/branches/{id}/delivery). Este e o valor de contingencia,
    usado quando aquela regra nao pode ser aplicada: rota indisponivel
    (Google fora do ar) ou filial sem base/por-km cadastrados. Ver
    DeliveryEstimateService._configured_fallback_fee.

    Zero desliga o fallback em vez de significar entrega gratis. A coluna
    tem default 0 e a maior parte das linhas nunca foi tocada; ler esse 0
    como escolha transformaria uma queda do Google em frete gratis para
    todo mundo.
    """

    min_order_value: Decimal | None = Field(default=None, ge=0)
    estimated_delivery_time_min: int | None = Field(default=None, ge=0, le=600)
    estimated_delivery_time_max: int | None = Field(default=None, ge=0, le=600)
    default_delivery_fee: Decimal | None = Field(default=None, ge=0)
    service_fee_enabled: bool | None = None
    service_fee_amount: Decimal | None = Field(default=None, ge=0)


class StoreStatusRequest(BaseModel):
    """Corpo do botao de abrir/fechar a loja (BLOCO C1).

    Rota propria, do mesmo jeito que a disponibilidade do produto: e a acao
    mais clicada do painel e nao pode arrastar junto o resto das
    configuracoes que estavam abertas na tela.

    O corpo nao mudou; o ALVO mudou. Era `PATCH /admin/settings/store-status`
    e fechava o restaurante inteiro; hoje e
    `PATCH /admin/branches/{branch_id}/store-status` e fecha uma loja.
    """

    is_open: bool


class AdminBranchOrderTypesRequest(BaseModel):
    """Quais tipos de pedido esta filial aceita agora.

    Separado de `store-status` porque sao gestos diferentes: fechar a loja e
    "nao estamos atendendo", desligar a entrega e "estamos atendendo, so nao
    entregamos" — o quiosque de shopping vive no segundo estado o dia
    inteiro, e o balcao sem motoboy cai nele no meio da tarde.

    Edicao parcial: mandar so `accepts_delivery` nao mexe na retirada.
    """

    accepts_delivery: bool | None = None
    accepts_pickup: bool | None = None

    @model_validator(mode="after")
    def validate_has_change(self):
        if self.accepts_delivery is None and self.accepts_pickup is None:
            raise ValueError("Informe accepts_delivery, accepts_pickup, ou os dois")
        return self


class AdminBranchSettingsUpdate(BaseModel):
    """As sobrescritas comerciais DESTA filial (revisao 20260818_0025).

    Tres estados por campo, e a diferenca entre os dois ultimos e o motivo de
    esta rota existir separada do PATCH de padroes:

    - **campo ausente do corpo** — nao mexe;
    - **campo com valor** — esta filial passa a usar esse valor;
    - **campo com `null` explicito** — esta filial VOLTA A HERDAR o padrao do
      restaurante.

    Sem o terceiro estado nao haveria como desfazer uma divergencia: a filial
    ficaria com a copia congelada para sempre, e mudar o padrao do
    restaurante nao chegaria nela.

    A validacao de par (minimo x maximo do prazo) roda sobre a MESCLA com o
    que ja esta no banco, pelo mesmo motivo de `AdminBranchDeliveryRules`.
    """

    min_order_value: Decimal | None = Field(default=None, ge=0)
    estimated_delivery_time_min: int | None = Field(default=None, ge=0, le=600)
    estimated_delivery_time_max: int | None = Field(default=None, ge=0, le=600)
    default_delivery_fee: Decimal | None = Field(default=None, ge=0)
    service_fee_enabled: bool | None = None
    service_fee_amount: Decimal | None = Field(default=None, ge=0)


class AdminBranchOperationOverrides(BaseResponse):
    """O que esta gravado NA FILIAL. Nulo significa "herda do restaurante".

    Existe ao lado de `effective` porque a tela precisa dos dois: o campo de
    edicao mostra a sobrescrita (vazio = herdando), e o texto ao lado mostra
    o valor que vai valer. Publicar so o efetivo faria toda filial parecer
    divergente; so a sobrescrita, faria toda filial parecer sem configuracao.
    """

    min_order_value: float | None = None
    estimated_delivery_time_min: int | None = None
    estimated_delivery_time_max: int | None = None
    default_delivery_fee: float | None = None
    service_fee_enabled: bool | None = None
    service_fee_amount: float | None = None


class AdminBranchOperationEffective(BaseResponse):
    """O que o PROXIMO PEDIDO desta filial vai usar. Filial mesclada com padrao."""

    min_order_value: float
    estimated_delivery_time_min: int | None = None
    estimated_delivery_time_max: int | None = None
    default_delivery_fee: float | None = None
    service_fee_enabled: bool
    service_fee_amount: float


class AdminBranchOperationResponse(BaseResponse):
    """Como uma filial esta operando agora — uma linha da tela de operacao.

    `is_open` e `is_open_now` sao coisas diferentes e as duas precisam
    aparecer: `is_open` e a chave que o lojista controla, `is_open_now`
    combina essa chave com a agenda da semana. Uma filial com `is_open=true`
    e `is_open_now=false` esta fora do horario cadastrado — a tela consegue
    dizer isso em vez de deixar o lojista achando que a loja esta no ar.
    """

    branch_id: UUID
    branch_name: str
    is_open: bool
    is_open_now: bool
    accepts_delivery: bool
    accepts_pickup: bool
    overrides: AdminBranchOperationOverrides
    effective: AdminBranchOperationEffective


class AdminBranchResponse(BaseResponse):
    id: UUID
    name: str
    slug: str
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    address: str
    neighborhood: str
    city: str
    state: str
    zipcode: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    delivery_base_fee: float | None = None
    delivery_fee_per_km: float | None = None
    delivery_min_fee: float | None = None
    delivery_max_fee: float | None = None
    delivery_max_distance_km: float | None = None
    is_main: bool | None = False
    is_active: bool | None = True


class AdminBranchDeliveryRules(BaseModel):
    """Regras de entrega da filial, validadas em conjunto.

    Ficam em uma classe propria porque a edicao e parcial e a validacao
    cruzada (minimo x maximo) tem que rodar sobre a MESCLA com o que ja
    esta no banco — mandar so `delivery_max_fee=5` para uma filial com
    minimo 8 e valido campo a campo e impossivel no conjunto.
    """

    delivery_base_fee: Decimal | None = Field(default=None, ge=0)
    delivery_fee_per_km: Decimal | None = Field(default=None, ge=0)
    delivery_min_fee: Decimal | None = Field(default=None, ge=0)
    delivery_max_fee: Decimal | None = Field(default=None, ge=0)
    delivery_max_distance_km: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_fee_range(self):
        if (
            self.delivery_min_fee is not None
            and self.delivery_max_fee is not None
            and self.delivery_max_fee < self.delivery_min_fee
        ):
            raise ValueError("delivery_max_fee nao pode ser menor que delivery_min_fee")
        return self


class AdminBranchUpdate(AdminBranchDeliveryRules):
    """Endereco, contato e regras de entrega da filial (BLOCO C3).

    Sem `slug` e sem `is_active`: o slug e URL publica e desativar filial e
    operacao de plataforma, nao de lojista — some do app de todo mundo e
    deixa pedido em aberto sem cozinha.

    Latitude e longitude entram porque sao a origem do calculo de rota: sem
    elas o Google mede a distancia a partir do lugar errado.
    """

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    display_name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=20)
    whatsapp: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, min_length=1, max_length=200)
    neighborhood: str | None = Field(default=None, min_length=1, max_length=120)
    city: str | None = Field(default=None, min_length=1, max_length=120)
    state: str | None = Field(default=None, min_length=1, max_length=40)
    zipcode: str | None = Field(default=None, max_length=20)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)


class BusinessHourInput(BaseModel):
    """Uma faixa de funcionamento de um dia da semana.

    `weekday` segue o mesmo 0=segunda do resto do projeto (o
    `datetime.weekday()` do Python, usado em BranchHoursService).

    Faixa que vira a noite (18:00 as 02:00) e valida e nao precisa de nada
    especial aqui: `closes_at` menor que `opens_at` ja significa isso para
    quem le (ver `_period_covers_after_midnight`).
    """

    weekday: int = Field(ge=0, le=6)
    opens_at: time | None = None
    closes_at: time | None = None
    prep_time_min: int | None = Field(default=None, ge=0, le=MAX_PREP_TIME_MINUTES)
    prep_time_max: int | None = Field(default=None, ge=0, le=MAX_PREP_TIME_MINUTES)
    is_closed: bool = False

    @model_validator(mode="after")
    def validate_period(self):
        if not self.is_closed and (self.opens_at is None or self.closes_at is None):
            raise ValueError("faixa aberta precisa de opens_at e closes_at")
        if self.opens_at is not None and self.opens_at == self.closes_at:
            # Sem isso a faixa nao cobre instante nenhum e a filial fica
            # "aberta" sem nunca aceitar pedido.
            raise ValueError("opens_at e closes_at nao podem ser iguais")
        if (
            self.prep_time_min is not None
            and self.prep_time_max is not None
            and self.prep_time_max < self.prep_time_min
        ):
            raise ValueError("prep_time_max nao pode ser menor que prep_time_min")
        return self


class BusinessHoursReplaceRequest(BaseModel):
    """A semana inteira da filial, de uma vez (BLOCO C2).

    Substitui tudo em vez de editar faixa por faixa porque a tela de
    horario e uma grade de sete dias que o lojista salva junto. Editar
    linha a linha exigiria id de faixa no painel e deixaria a semana pela
    metade se uma das chamadas falhasse.

    Dia ausente da lista = dia fechado.
    """

    periods: list[BusinessHourInput] = Field(
        default_factory=list, max_length=7 * MAX_PERIODS_PER_WEEKDAY
    )


class BusinessHourResponse(BaseResponse):
    id: UUID
    weekday: int
    opens_at: time | None = None
    closes_at: time | None = None
    prep_time_min: int | None = None
    prep_time_max: int | None = None
    is_closed: bool
    sort_order: int


class BranchPrepTimeAdjustRequest(BaseModel):
    """Ajuste do tempo de preparo que esta valendo AGORA.

    Existe separado do PUT da semana porque sao gestos diferentes: aquele e
    o cadastro (uma tela, sete dias, salvo com calma), este e o botao que o
    atendente aperta no meio do almoco quando a fila cresceu.

    Dois modos, um por vez:

    - `delta_minutes` — o atalho de +5/-10. Desloca a janela inteira a
      partir do que ja esta gravado.
    - `prep_time_min` + `prep_time_max` — valor absoluto, para a faixa que
      ainda nao tem prazo nenhum: sem base, um delta nao tem de onde partir.

    O par absoluto anda junto pelo mesmo motivo de `AdminBranchDeliveryRules`:
    mandar so o maximo deixaria a faixa com teto abaixo do piso.
    """

    delta_minutes: int | None = Field(
        default=None,
        ge=-MAX_PREP_TIME_DELTA_MINUTES,
        le=MAX_PREP_TIME_DELTA_MINUTES,
    )
    prep_time_min: int | None = Field(default=None, ge=0, le=MAX_PREP_TIME_MINUTES)
    prep_time_max: int | None = Field(default=None, ge=0, le=MAX_PREP_TIME_MINUTES)

    @model_validator(mode="after")
    def validate_single_mode(self):
        absolute_fields = (self.prep_time_min, self.prep_time_max)
        has_absolute = any(value is not None for value in absolute_fields)

        if self.delta_minutes is not None and has_absolute:
            # Os dois juntos nao tem resposta obvia: o delta se aplicaria
            # sobre o valor novo ou sobre o que estava no banco?
            raise ValueError(
                "Informe delta_minutes ou o par prep_time_min/prep_time_max, nao os dois"
            )
        if self.delta_minutes is None and not has_absolute:
            raise ValueError("Informe delta_minutes ou o par prep_time_min/prep_time_max")
        if has_absolute and any(value is None for value in absolute_fields):
            raise ValueError("prep_time_min e prep_time_max andam juntos")
        if has_absolute and self.prep_time_max < self.prep_time_min:
            raise ValueError("prep_time_max nao pode ser menor que prep_time_min")
        return self


class AdminPaymentMethodResponse(BaseResponse):
    id: UUID
    branch_id: UUID
    payment_flow: PaymentFlow
    method_type: PaymentMethodType
    brand: str | None = None
    label: str
    icon_key: str | None = None
    enabled: bool
    requires_gateway: bool
    sort_order: int
    notes: str | None = None


class AdminPaymentMethodCreate(BaseModel):
    """Forma de pagamento habilitada em uma filial (BLOCO C4).

    `payment_flow` decide se o pedido nasce esperando o gateway ou se o
    dinheiro entra na entrega — e por isso que ele e do contrato e nao
    deduzido do `method_type`: a mesma bandeira de cartao pode ser cobrada
    online (gateway) ou na maquininha do entregador.
    """

    payment_flow: PaymentFlow
    method_type: PaymentMethodType
    label: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    brand: str | None = Field(default=None, max_length=60)
    icon_key: str | None = Field(default=None, max_length=60)
    enabled: bool = True
    requires_gateway: bool = False
    sort_order: int = Field(default=0, ge=0)
    notes: str | None = Field(default=None, max_length=300)

    @field_validator("label")
    @classmethod
    def strip_label(cls, value: str) -> str:
        return value.strip()


class AdminPaymentMethodUpdate(BaseModel):
    """Edicao parcial. `payment_flow` e `method_type` nao mudam.

    Trocar o fluxo de uma forma ja cadastrada mudaria, no meio do
    expediente, como os proximos pedidos daquela filial sao cobrados. Quem
    errou o cadastro desabilita a linha e cria outra — o historico dos
    pedidos ja fechados continua fazendo sentido.
    """

    label: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    brand: str | None = Field(default=None, max_length=60)
    icon_key: str | None = Field(default=None, max_length=60)
    enabled: bool | None = None
    requires_gateway: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=300)

    @field_validator("label")
    @classmethod
    def strip_label(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None
