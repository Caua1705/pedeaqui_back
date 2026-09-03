from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.schemas.admin_customer_schema import CustomerSegment
from src.schemas.common_schema import BaseResponse


DiscountType = Literal["fixed", "percent", "free_delivery"]


class CouponVisibility(str, Enum):
    """Quem enxerga o cupom. Substituiu o `is_public` na revisao 20260828_0043.

    `str, Enum` e nao `Literal` pelo mesmo motivo de `PaymentErrorCode` e de
    `CustomerSegment`: so assim a LISTA de valores sai nomeada no
    `/openapi.json`, e o painel gera o seletor a partir do documento em vez
    de decorar tres strings.

    Os valores espelham o CHECK `ck_restaurant_coupons_visibility`.
    """

    PUBLIC = "public"
    SEGMENT = "segment"
    PRIVATE = "private"


class CustomerCouponLabel(str, Enum):
    """A etiqueta que o app pinta no card do cupom.

    **So existe UMA etiqueta, e cupom publico vem sem nenhuma.** Se todo
    mundo ve, "para todos" e ruido no card — o espaco e caro e a frase nao
    informa nada que a presenca do cupom na lista ja nao diga.

    E ela **nunca fala de disponibilidade**. "Selecionado para voce" diz de
    quem e a campanha, e nao se da para usar agora; quem responde isso e o
    `state`. Sem essa separacao a etiqueta e o botao se contradizem na mesma
    tela — "selecionado para voce" ao lado de "faltam R$ 12".

    Nao ha `exclusivo`: o alvo e um SEGMENTO, nao uma pessoa, e prometer
    exclusividade para um recorte de milhares de clientes e propaganda que
    nao se sustenta.
    """

    SELECTED_FOR_YOU = "selected_for_you"


class CustomerCouponState(str, Enum):
    """O que o botao do card pode fazer com ESTA sacola.

    Nao ha valor para "nao aparece": cupom sem conserto nesta sacola —
    vencido, primeira-compra para quem ja comprou, de outro segmento, teto
    estourado, cooldown correndo — simplesmente nao entra na lista. Um card
    cinza com "voce nao pode usar" so ocupa a tela com uma negativa que a
    pessoa nao tem como resolver.

    O que ENTRA e o que ela consegue mudar agora: colocar mais coisa na
    sacola (`missing_amount`) ou entrar na conta (`login_required`).
    """

    APPLICABLE = "applicable"
    MISSING_AMOUNT = "missing_amount"
    LOGIN_REQUIRED = "login_required"


class PublicCouponResponse(BaseResponse):
    """Legacy menu contract. Eligibility must be checked by the coupon endpoints."""

    id: UUID
    # Nulo quando o cupom aplica sozinho no checkout. A vitrine do cardapio
    # mostra o card do mesmo jeito; o que muda e nao haver codigo a copiar.
    code: str | None = None
    name: str
    image_path: str | None = None
    image_url: str | None = None
    discount_type: str
    discount_value: float
    min_order_value: float
    sort_order: int
    is_active: bool


class CouponTemplateResponse(BaseResponse):
    """A arte da vitrine, para o painel montar o seletor do POST /admin/coupons.

    `coupon_template_id` e obrigatorio na criacao do cupom e os templates sao
    da PLATAFORMA, nao do restaurante: nao ha rota que os cadastre e nao ha
    coluna `restaurant_id` neles. Por isso a lista vem inteira, sem recorte.

    `image_url` acompanha `image_path` pelo mesmo motivo de
    `PublicCouponResponse`: o caminho sozinho nao renderiza — quem monta a URL
    do bucket e o backend (`build_storage_url`), e duplicar essa regra no
    painel seria a segunda copia da configuracao do Supabase.
    """

    id: UUID
    name: str
    image_path: str | None = None
    image_url: str | None = None
    discount_type: str
    discount_value: Decimal | None = None
    sort_order: int


class CouponSelector(BaseModel):
    coupon_id: UUID | None = None
    coupon_code: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("coupon_code")
    @classmethod
    def normalize_code(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None

    @model_validator(mode="after")
    def validate_single_coupon(self):
        if self.coupon_id is not None and self.coupon_code is not None:
            raise ValueError("Informe somente coupon_id ou coupon_code")
        return self


class CouponCampaignFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coupon_template_id: UUID
    # OPCIONAL desde 28/08/2026, e o nulo e uma escolha do lojista, nao um
    # campo em branco: **cupom sem codigo aplica sozinho no checkout** quando
    # a sacola permite; cupom com codigo exige que a pessoa digite.
    code: str | None = Field(default=None, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    discount_type: DiscountType
    discount_value: Decimal = Field(ge=0)
    max_discount_amount: Decimal | None = Field(default=None, ge=0)
    min_order_value: Decimal = Field(default=Decimal("0.00"), ge=0)
    valid_from: datetime
    # NULO = A CAMPANHA NAO EXPIRA. Nao e campo em branco nem dado faltando:
    # e "10% no canal proprio, sem prazo", e o precedente esta duas dezenas de
    # linhas acima — `code` nulo significa "aplica sozinho" desde a revisao
    # `20260828_0043`.
    #
    # Do lado do PATCH isso ja funciona sem nada a mais: `update_admin` usa
    # `exclude_unset=True`, entao `{"valid_until": null}` TIRA o prazo e um
    # PATCH que nao mande o campo preserva o que esta gravado. Mesma mecanica
    # do `code`.
    #
    # Quem le a janela e `src/services/coupon_window.py`, nunca este campo
    # direto.
    valid_until: datetime | None = None
    total_usage_limit: int | None = Field(default=None, ge=1)
    usage_limit_per_customer: int | None = Field(default=None, ge=1)
    cooldown_days: int | None = Field(default=None, ge=1)
    first_order_only: bool = False
    visibility: CouponVisibility = CouponVisibility.PUBLIC
    target_segment: CustomerSegment | None = None
    is_active: bool = True
    # A posicao na vitrine. As duas consultas publicas ja ordenavam por ela
    # desde sempre; o painel e que nao tinha como escrever o valor, entao todo
    # cupom ficava no `DEFAULT 0` da coluna e a ordem saia do desempate.
    #
    # `ge=0` e nao positivo: zero e a posicao normal de quem nunca foi
    # arrastado, e recusa-lo tornaria invalido o valor que TODO cupom de hoje
    # ja tem gravado — um PATCH de `is_active` passaria a dar 422 por causa de
    # um campo que o lojista nem tocou (o merge valida o cupom inteiro).
    sort_order: int = Field(default=0, ge=0)

    @field_validator("code")
    @classmethod
    def normalize_campaign_code(cls, value: str | None) -> str | None:
        """Nulo passa; espaco em branco NAO vira nulo.

        A diferenca decide comportamento de produto, e por isso o branco e
        recusado em vez de convertido: `None` significa "aplica sozinho", e
        um lojista que apagou o campo por engano teria criado, calado, um
        desconto automatico para a loja inteira.

        Antes desta funcao o codigo em branco chegava ao banco, batia no
        CHECK `restaurant_coupons_code_not_blank` e voltava como "codigo ja
        existe" — 409 para um cupom que nao existia.
        """
        if value is None:
            return None
        code = value.strip().upper()
        if not code:
            raise ValueError("code não pode ser só espaços; omita o campo para o cupom aplicar sozinho")
        return code

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_campaign(self):
        # `valid_until` nulo nao tem ordem a respeitar: campanha sem fim nao
        # pode terminar antes de comecar.
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until deve ser posterior a valid_from")
        if self.discount_type in {"fixed", "percent"} and self.discount_value <= 0:
            raise ValueError("discount_value deve ser maior que zero")
        if self.discount_type == "percent" and self.discount_value > 100:
            raise ValueError("discount_value percentual deve ser no máximo 100")
        if self.discount_type != "percent" and self.max_discount_amount is not None:
            raise ValueError("max_discount_amount é permitido somente para percentual")
        # Espelha o CHECK `restaurant_coupons_reuse_rules_valid`. Um cupom que o
        # cliente so pode usar UMA vez na vida nao tem o que fazer com intervalo
        # entre usos: a segunda vez nunca chega. O banco ja recusava, mas a
        # recusa vinha de la sem dizer qual dos dois campos estava sobrando.
        if self.cooldown_days is not None and self.usage_limit_per_customer == 1:
            raise ValueError("cooldown_days não faz sentido com usage_limit_per_customer = 1")
        # Espelha o CHECK `ck_restaurant_coupons_segment_needs_target`, nos
        # DOIS sentidos. O segundo e o que costuma faltar: alvo preenchido
        # num cupom publico nao filtra nada, e o lojista veria na tela uma
        # segmentacao que a lista ignora.
        if self.visibility is CouponVisibility.SEGMENT and self.target_segment is None:
            raise ValueError("cupom de segmento precisa de target_segment")
        if self.visibility is not CouponVisibility.SEGMENT and self.target_segment is not None:
            raise ValueError("target_segment só vale com visibility = segment")
        return self


class CouponCreate(CouponCampaignFields):
    pass


class CouponUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coupon_template_id: UUID | None = None
    code: str | None = Field(default=None, max_length=100)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    discount_type: DiscountType | None = None
    discount_value: Decimal | None = Field(default=None, ge=0)
    max_discount_amount: Decimal | None = Field(default=None, ge=0)
    min_order_value: Decimal | None = Field(default=None, ge=0)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    total_usage_limit: int | None = Field(default=None, ge=1)
    usage_limit_per_customer: int | None = Field(default=None, ge=1)
    cooldown_days: int | None = Field(default=None, ge=1)
    first_order_only: bool | None = None
    visibility: CouponVisibility | None = None
    target_segment: CustomerSegment | None = None
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)

    @field_validator("code")
    @classmethod
    def normalize_update_code(cls, value: str | None) -> str | None:
        """`{"code": null}` TIRA o codigo do cupom — nao e "campo nao enviado".

        Quem decide se o campo veio e o `exclude_unset=True` do
        `update_admin`, e nao este validator. Ou seja: PATCH que nao mande
        `code` preserva o que esta gravado; PATCH que mande `null`
        transforma a campanha em automatica, que e uma decisao de produto e
        precisa de um jeito de ser tomada.
        """
        return value.strip().upper() if value else None

    @field_validator("title")
    @classmethod
    def normalize_update_title(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class CouponAdminResponse(BaseResponse):
    """O cupom como o painel do lojista o le.

    `total_usage_count` e o par de `total_usage_limit`, e conta a mesma coisa
    que `evaluate` conta para decidir se o cupom ainda vale: redencoes em
    `applied`, so. Redencao estornada (o pedido foi cancelado) devolve a vaga e
    sai da conta — a tela mostra o numero que barra o proximo cliente, nao um
    historico de tentativas. Sem ele o painel exibia "limite: 100" e nao sabia
    quantos ja tinham usado.

    OPCIONAL com default, e nao obrigatorio: o numero e preenchido pelo
    service, nao sai de coluna nenhuma, entao um `model_validate(coupon)` novo
    que esqueca de passa-lo devolve `null` em vez de estourar na serializacao.
    """

    id: UUID
    restaurant_id: UUID
    coupon_template_id: UUID
    code: str | None = None
    title: str
    description: str | None = None
    discount_type: DiscountType
    discount_value: Decimal
    max_discount_amount: Decimal | None = None
    min_order_value: Decimal
    valid_from: datetime
    # Nulo = campanha sem prazo. O painel precisa saber distinguir isso de
    # "campo nao preenchido": o card diz "sem prazo" em vez de data vazia.
    valid_until: datetime | None = None
    total_usage_limit: int | None = None
    usage_limit_per_customer: int | None = None
    cooldown_days: int | None = None
    first_order_only: bool
    visibility: CouponVisibility
    target_segment: CustomerSegment | None = None
    is_active: bool
    # Sem devolver a posicao atual, o painel nao tem como desenhar a lista na
    # ordem que ele acabou de gravar — teria que reordenar por conta e as duas
    # telas voltariam a discordar.
    sort_order: int
    total_usage_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CustomerCouponResponse(BaseModel):
    """Um cupom no app do cliente, com o estado JA DECIDIDO pelo backend.

    O front nao calcula nada aqui — nem o desconto, nem se cabe, nem quanto
    falta. Ele pinta `label`, pinta `state` e mostra `discount_amount`.

    **Por que a conta nao pode ser do lado de la.** Ela depende de
    `max_discount_amount`, do teto por cliente, do cooldown, do
    primeira-compra e de qual parte da sacola o desconto morde
    (`free_delivery` desconta a taxa, os outros dois descontam o subtotal).
    Reproduzir isso no app seria a segunda implementacao da regra de cupom, e
    a divergencia apareceria onde mais custa: o card promete R$ 15, o
    checkout tira R$ 10, e o cliente ve o desconto encolher entre uma tela e
    a outra.

    ## O que NAO vem aqui, e nao e esquecimento

    `discount_value`, `max_discount_amount`, `total_usage_limit`,
    `usage_limit_per_customer` e `cooldown_days` sao PARAMETROS da conta e
    limites internos da campanha. Quem precisa deles e quem calcula, e quem
    calcula e o backend. Publicados, so serviriam para alguem refazer a
    conta errado — ou para mapear os limites da campanha de fora.

    `min_order_value` fica, porque ele nao e parametro: e a frase "pedido
    minimo R$ 30" que o card precisa escrever. E `valid_until` fica pelo
    mesmo motivo — "valido ate" e texto de card.
    """

    id: UUID
    # NULO quando o cupom aplica sozinho no checkout. E a diferenca que o
    # card mostra: com codigo, ha o que copiar; sem codigo, o desconto ja
    # entra quando a sacola permitir.
    code: str | None = None
    title: str
    description: str | None = None
    # A arte da vitrine, ja como URL do bucket. O `image_path` cru nao vem:
    # quem monta a URL e o backend (`build_storage_url`), e duplicar essa
    # regra no app seria a segunda copia da configuracao do Supabase.
    image_url: str | None = None
    discount_type: DiscountType
    min_order_value: Decimal
    # Nulo = campanha sem prazo, e o app escreve "sem prazo" em vez de esconder
    # o card. Antes deste `| None`, um cupom permanente derrubava a LISTA
    # inteira do cliente na serializacao, e nao so o proprio card.
    valid_until: datetime | None = None

    # Nulo em cupom publico. Ver `CustomerCouponLabel`.
    label: CustomerCouponLabel | None = None
    state: CustomerCouponState
    # O que ESTE cupom tiraria DESTA sacola. Zero quando o estado nao e
    # `applicable` — e nao o desconto hipotetico de uma sacola maior, que
    # faria o card anunciar um valor que o checkout nao vai dar.
    discount_amount: Decimal
    # Quanto falta no subtotal para o cupom passar a valer. Zero quando ja
    # cabe. E o unico numero que transforma "nao da" em "faltam R$ 12".
    missing_amount: Decimal


class CustomerCouponsResponse(BaseModel):
    coupons: list[CustomerCouponResponse]


class CouponClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)

    @field_validator("code")
    @classmethod
    def normalize_claim_code(cls, value: str) -> str:
        code = value.strip().upper()
        if not code:
            raise ValueError("Informe o código do cupom")
        return code


class CouponClaimResponse(BaseModel):
    """O cupom que acabou de virar do cliente.

    Vem no MESMO formato da lista, e nao numa resposta propria, para o app
    poder inserir o card resgatado direto na tela sem uma segunda chamada —
    e para nao existirem duas descricoes de cupom que precisem concordar.

    O `state` aqui e calculado sobre uma sacola VAZIA (o resgate acontece no
    Clube, sem carrinho), entao um cupom com pedido minimo volta como
    `missing_amount` com o minimo inteiro faltando. Isso e o certo: ele foi
    resgatado, ele e do cliente, e ainda nao cabe.
    """

    coupon: CustomerCouponResponse


class CouponPreviewRequest(CouponSelector):
    model_config = ConfigDict(extra="forbid")

    subtotal: Decimal = Field(ge=0)
    delivery_fee: Decimal = Field(default=Decimal("0.00"), ge=0)
    order_type: str

    @model_validator(mode="after")
    def require_coupon(self):
        if self.coupon_id is None and self.coupon_code is None:
            raise ValueError("Informe coupon_id ou coupon_code")
        return self


class CouponPreviewResponse(BaseModel):
    valid: bool
    coupon_id: UUID
    # Nulo quando o cupom e automatico (sem codigo). O preview dele so e
    # alcancavel por `coupon_id` — nao ha codigo a digitar —, e antes de
    # ficar opcional este campo devolvia 500 na serializacao.
    coupon_code: str | None = None
    discount_type: DiscountType
    discount_amount: Decimal
    subtotal: Decimal
    delivery_fee: Decimal
    total_after_coupon: Decimal
    ineligibility_reason: str | None = None
    next_available_at: datetime | None = None
