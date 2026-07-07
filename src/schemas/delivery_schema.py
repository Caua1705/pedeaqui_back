from decimal import Decimal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


DEFAULT_DELIVERY_CITY = "Fortaleza"
DEFAULT_DELIVERY_STATE = "CE"


class DeliveryAddressInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    street: str = Field(min_length=1)
    number: str = Field(min_length=1)
    neighborhood: str = Field(min_length=1)
    city: str | None = DEFAULT_DELIVERY_CITY
    state: str | None = DEFAULT_DELIVERY_STATE
    zipcode: str | None = Field(
        default=None,
        validation_alias=AliasChoices("zipcode", "zip_code"),
    )
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)

    @field_validator("street", "number", "neighborhood", mode="before")
    @classmethod
    def normalize_required_text(cls, value, info):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{info.field_name} deve ser informado")
        return value.strip()

    @field_validator("city", "state", mode="before")
    @classmethod
    def apply_temporary_location_fallback(cls, value, info):
        # Fallback temporario enquanto clientes antigos podem omitir cidade/UF.
        if value is None or (isinstance(value, str) and not value.strip()):
            return DEFAULT_DELIVERY_CITY if info.field_name == "city" else DEFAULT_DELIVERY_STATE
        return value.strip() if isinstance(value, str) else value

    @field_validator("zipcode", mode="before")
    @classmethod
    def normalize_optional_zipcode(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def normalize_optional_coordinate(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value


class DeliveryEstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_id: UUID | None = None
    address_id: UUID | None = None
    address: DeliveryAddressInput | None = None

    @field_validator("branch_id", "address_id", mode="before")
    @classmethod
    def normalize_optional_uuid(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_address_source(self):
        if (self.address_id is None) == (self.address is None):
            raise ValueError("Informe exatamente um entre address_id e address")
        return self


class DeliveryEstimateResponse(BaseModel):
    serviceable: bool
    reason: str | None = None
    message: str | None = None
    distance_km: float | None = None
    travel_time_min: int | None = None
    prep_time_min: int | None = None
    prep_time_max: int | None = None
    eta_min: int | None = None
    eta_max: int | None = None
    delivery_fee: float | None = None
    provider: str
    fallback: bool = False
