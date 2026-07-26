import uuid

from pydantic import BaseModel, ConfigDict, Field


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=200)


class AdminUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    restaurant_id: uuid.UUID
    branch_id: uuid.UUID | None
    name: str
    email: str
    role: str
    is_active: bool


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    admin_user: AdminUserResponse
