from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.schemas.common_schema import BaseResponse


DiscountType = Literal["fixed", "percent", "free_delivery"]


class PublicCouponResponse(BaseResponse):
    """Legacy menu contract. Eligibility must be checked by the coupon endpoints."""

    id: UUID
    code: str
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
    code: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    discount_type: DiscountType
    discount_value: Decimal = Field(ge=0)
    max_discount_amount: Decimal | None = Field(default=None, ge=0)
    min_order_value: Decimal = Field(default=Decimal("0.00"), ge=0)
    valid_from: datetime
    valid_until: datetime
    total_usage_limit: int | None = Field(default=None, ge=1)
    usage_limit_per_customer: int | None = Field(default=None, ge=1)
    cooldown_days: int | None = Field(default=None, ge=1)
    first_order_only: bool = False
    is_public: bool = True
    is_active: bool = True

    @field_validator("code")
    @classmethod
    def normalize_campaign_code(cls, value: str) -> str:
        code = value.strip().upper()
        # `min_length=1` conferiu o valor CRU: "   " tem tres caracteres e
        # passa, e so aqui vira vazio. Sem esta linha o codigo em branco chegava
        # ao banco, batia no CHECK `restaurant_coupons_code_not_blank` e voltava
        # como "codigo ja existe" — 409 para um cupom que nao existia.
        if not code:
            raise ValueError("code não pode ser só espaços")
        return code

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_campaign(self):
        if self.valid_until <= self.valid_from:
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
        return self


class CouponCreate(CouponCampaignFields):
    pass


class CouponUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coupon_template_id: UUID | None = None
    code: str | None = Field(default=None, min_length=1, max_length=100)
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
    is_public: bool | None = None
    is_active: bool | None = None

    @field_validator("code")
    @classmethod
    def normalize_update_code(cls, value: str | None) -> str | None:
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
    code: str
    title: str
    description: str | None = None
    discount_type: DiscountType
    discount_value: Decimal
    max_discount_amount: Decimal | None = None
    min_order_value: Decimal
    valid_from: datetime
    valid_until: datetime
    total_usage_limit: int | None = None
    usage_limit_per_customer: int | None = None
    cooldown_days: int | None = None
    first_order_only: bool
    is_public: bool
    is_active: bool
    total_usage_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AvailableCouponResponse(BaseModel):
    id: UUID
    code: str
    title: str
    description: str | None = None
    discount_type: DiscountType
    discount_value: Decimal
    max_discount_amount: Decimal | None = None
    min_order_value: Decimal
    valid_until: datetime
    cooldown_days: int | None = None
    eligible: bool
    requires_login: bool = False
    estimated_discount: Decimal
    missing_amount: Decimal
    ineligibility_reason: str | None = None
    next_available_at: datetime | None = None


class AvailableCouponsResponse(BaseModel):
    coupons: list[AvailableCouponResponse]


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
    coupon_code: str
    discount_type: DiscountType
    discount_amount: Decimal
    subtotal: Decimal
    delivery_fee: Decimal
    total_after_coupon: Decimal
    ineligibility_reason: str | None = None
    next_available_at: datetime | None = None
