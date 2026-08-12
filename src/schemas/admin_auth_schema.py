import uuid

from pydantic import BaseModel, ConfigDict, Field

from src.core.constants import MIN_ADMIN_PASSWORD_LENGTH


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    # `min_length=1` no LOGIN, e nao o minimo real: quem ja tem uma senha
    # curta cadastrada precisa conseguir entrar para poder troca-la. Validar o
    # tamanho aqui trancaria a conta do lado de fora.
    password: str = Field(min_length=1, max_length=200)


class ChangeAdminPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=200)
    # O minimo e cobrado aqui, na ESCRITA. O 400 do Pydantic ja diz o tamanho
    # exigido, entao o painel nao precisa repetir a regra.
    new_password: str = Field(min_length=MIN_ADMIN_PASSWORD_LENGTH, max_length=200)
    confirm_password: str = Field(min_length=1, max_length=200)


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
