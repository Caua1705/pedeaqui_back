from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from src.schemas.common_schema import BaseResponse
from src.schemas.order_schema import OrderItemResponse
from src.utils.normalization import (
    is_valid_email,
    normalize_digits,
    normalize_email,
    normalize_text,
)


class CustomerAddressBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    label: str | None = None
    street: str | None = None
    number: str | None = None
    neighborhood: str | None = None
    complement: str | None = None
    reference: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)

    @field_validator("zipcode")
    @classmethod
    def normalize_zipcode(cls, value: str | None) -> str | None:
        return normalize_digits(value) if value else value


class CreateCustomerAddressRequest(CustomerAddressBase):
    street: str = Field(min_length=1)
    number: str = Field(min_length=1)
    neighborhood: str = Field(min_length=1)
    is_default: bool = False


class UpdateCustomerAddressRequest(CustomerAddressBase):
    is_default: bool | None = None


class CustomerAddressResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    customer_id: UUID
    client_reference: str | None = None
    label: str | None = None
    street: str
    number: str
    neighborhood: str
    complement: str | None = None
    reference: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    is_default: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ImportCustomerAddressRequest(CustomerAddressBase):
    zipcode: str | None = Field(
        default=None,
        validation_alias=AliasChoices("zipcode", "zip_code"),
    )
    client_reference: str | None = Field(default=None, min_length=1, max_length=100)
    street: str = Field(min_length=1)
    number: str = Field(min_length=1)
    neighborhood: str = Field(min_length=1)
    is_default: bool = False

    @field_validator("client_reference", mode="before")
    @classmethod
    def normalize_client_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("street", "number", "neighborhood", mode="before")
    @classmethod
    def normalize_required_address_text(cls, value, info):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{info.field_name} deve ser informado")
        return value.strip()

    @field_validator("label", "complement", "reference", "city", "state", "zipcode", mode="before")
    @classmethod
    def normalize_optional_address_text(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def normalize_optional_address_coordinate(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value


class ImportCustomerAddressesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addresses: list[ImportCustomerAddressRequest] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_single_default(self):
        if sum(address.is_default for address in self.addresses) > 1:
            raise ValueError("Apenas um endereco pode ser definido como padrao")
        return self


class IgnoredImportedAddress(BaseModel):
    client_reference: str | None = None
    reason: str


class ImportCustomerAddressesResponse(BaseModel):
    created: list[CustomerAddressResponse] = Field(default_factory=list)
    existing: list[CustomerAddressResponse] = Field(default_factory=list)
    ignored: list[IgnoredImportedAddress] = Field(default_factory=list)

class CurrentCustomerResponse(BaseResponse):
    id: UUID
    name: str
    email: str
    phone: str
    # `cpf` saiu daqui na frente 5 (LGPD). Era o unico lugar da API que
    # devolvia o documento, e ele nao era usado para mais nada — a coluna foi
    # anulada pela revisao 0019. Publicar campo que hoje seria sempre nulo so
    # convidaria a tela a mostrar espaco vazio.
    birth_date: date
    email_verified: bool
    marketing_opt_in: bool


class UpdateCurrentCustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    email: str
    phone: str
    birth_date: date
    # Opcional, e nao obrigatorio como os quatro acima, por duas razoes:
    #
    # 1. campo obrigatorio novo em request que o painel e o app ja mandam faz
    #    toda versao instalada passar a receber 422 ate ser atualizada
    #    (armadilha 7: campo com default e de graca, obrigatorio nao);
    # 2. ausente e "nao mexi no consentimento", que e diferente de `false`
    #    ("revoguei"). Sem a distincao, um cliente que editasse o telefone
    #    revogaria o proprio opt-in sem pedir.
    #
    # Era o unico jeito de revogar: o consentimento era coletado no cadastro
    # e o `extra="forbid"` acima impedia qualquer alteracao depois.
    marketing_opt_in: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        # NFC alem do strip: `customer_name_snapshot` e procurado com ILIKE
        # no painel, e ILIKE compara bytes — "Antônio" decomposto nao seria
        # encontrado por quem digita a forma composta.
        name = normalize_text(value)
        if not name:
            raise ValueError("name is required")
        return name

    @field_validator("email")
    @classmethod
    def validate_and_normalize_email(cls, value: str) -> str:
        email = normalize_email(value)
        if not is_valid_email(email):
            raise ValueError("invalid email")
        return email

    @field_validator("phone")
    @classmethod
    def validate_and_normalize_phone(cls, value: str) -> str:
        phone = normalize_digits(value)
        if len(phone) < 8:
            raise ValueError("invalid phone")
        return phone


class ChangeCustomerPasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class CustomerOrderHistoryItem(BaseModel):
    id: UUID
    order_number: int
    restaurant_name: str
    branch_name: str
    status: str
    order_type: str
    subtotal: float
    delivery_fee: float
    service_fee: float
    coupon_code: str | None = None
    coupon_discount_amount: Decimal = Decimal("0.00")
    cashback_redeemed_amount: Decimal = Decimal("0.00")
    discount_total: Decimal = Decimal("0.00")
    total: float
    created_at: datetime | None = None
    items: list[OrderItemResponse]
