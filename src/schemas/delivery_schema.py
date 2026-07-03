from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeliveryAddressInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    street: str = Field(min_length=1)
    number: str = Field(min_length=1)
    neighborhood: str = Field(min_length=1)
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)


class DeliveryEstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_id: UUID | None = None
    address_id: UUID | None = None
    address: DeliveryAddressInput | None = None

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
    eta_min: int | None = None
    eta_max: int | None = None
    delivery_fee: float | None = None
    provider: str
    fallback: bool = False
