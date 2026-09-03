from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from src.utils.normalization import normalize_digits, normalize_email


class RegisterCustomerRequest(BaseModel):
    name: str = Field(min_length=1)
    email: str
    phone: str = Field(min_length=8)
    birth_date: date
    password: str
    marketing_opt_in: bool = False
    privacy_accepted: bool

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("phone")
    @classmethod
    def normalize_digits_value(cls, value: str) -> str:
        return normalize_digits(value)


class RegisterCustomerResponse(BaseModel):
    customer_id: UUID
    email: str
    requires_email_verification: bool
    message: str


class VerifyEmailCodeRequest(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)
    # O ticket do "entrar com Google", caso (b): o `sub` novo cujo e-mail JA
    # TEM conta. Quando ele vem, o codigo certo LIGA a identidade ao cliente
    # existente e a resposta traz o JWT; quando nao vem, esta rota faz
    # exatamente o que sempre fez.
    #
    # OPCIONAL, e o comportamento diferente e PEDIDO por quem chama — nao
    # deduzido de estado escondido no servidor. E a diferenca que decide se
    # "o mesmo endpoint com comportamento diferente" e legivel ou e uma
    # armadilha: o cadastro por e-mail nunca manda este campo, entao ele nao
    # consegue cair no outro caminho por acidente.
    google_link_ticket: str | None = None

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, value: str) -> str:
        return normalize_email(value)


class ResendEmailCodeRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, value: str) -> str:
        return normalize_email(value)


class LoginRequest(BaseModel):
    login: str
    password: str


class LoginCustomerResponse(BaseModel):
    id: UUID
    name: str
    email: str
    phone: str
    email_verified: bool


class LoginResponse(BaseModel):
    access_token: str | None = None
    token_type: str | None = None
    customer: LoginCustomerResponse | None = None
    requires_email_verification: bool = False
    email: str | None = None
    message: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, value: str) -> str:
        return normalize_email(value)


class VerifyResetCodeRequest(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, value: str) -> str:
        return normalize_email(value)


class VerifyResetCodeResponse(BaseModel):
    reset_token: str


class ResetPasswordRequest(BaseModel):
    reset_token: str
    new_password: str
    confirm_password: str


class MessageResponse(BaseModel):
    message: str


class VerifyEmailCodeResponse(BaseModel):
    verified: bool
    message: str
    # Preenchidos SO quando veio um `google_link_ticket` e a ligacao foi
    # feita. Campo novo com DEFAULT, entao nao custa nada a quem ja consome
    # esta resposta (armadilha 7: obrigatorio custaria).
    #
    # O JWT sai aqui porque a alternativa era pior: sem ele, o app teria que
    # chamar `POST /auth/google` de novo com o mesmo `id_token` so para
    # receber a sessao — uma ida a mais, num token que pode ter vencido no
    # meio tempo.
    linked_provider: str | None = None
    access_token: str | None = None
    token_type: str | None = None
    customer: LoginCustomerResponse | None = None


# --- Entrar com Google -------------------------------------------------------
#
# Os tres desfechos de `POST /auth/google`, e eles espelham os tres casos:
#
#   authenticated               (a) o `sub` ja e conhecido -> sessao
#   link_confirmation_required  (b) `sub` novo, e-mail COM conta -> codigo
#   profile_required            (c) `sub` novo, e-mail sem conta -> cadastro
GOOGLE_AUTHENTICATED = "authenticated"
GOOGLE_LINK_CONFIRMATION_REQUIRED = "link_confirmation_required"
GOOGLE_PROFILE_REQUIRED = "profile_required"

GoogleSignInStatus = Literal[
    "authenticated",
    "link_confirmation_required",
    "profile_required",
]


class GoogleSignInRequest(BaseModel):
    # O teto existe para um `id_token` gigante nao chegar ao PyJWT: o corpo
    # ja tem limite geral, mas este e o campo que vira trabalho de crypto.
    id_token: str = Field(min_length=1, max_length=8192)


class GoogleSignInResponse(BaseModel):
    """Uma resposta com campos opcionais, e nao tres schemas.

    E a forma que `LoginResponse` ja usa nesta API, e o app do cliente ja sabe
    ler. `status` diz qual bloco esta preenchido.
    """

    status: GoogleSignInStatus
    message: str
    email: str | None = None

    # status == "authenticated"
    access_token: str | None = None
    token_type: str | None = None
    customer: LoginCustomerResponse | None = None

    # status == "link_confirmation_required": mande o ticket de volta em
    # `POST /auth/verify-email-code`, junto do codigo que chegou no e-mail.
    link_ticket: str | None = None

    # status == "profile_required": mande o ticket em
    # `POST /auth/google/complete-signup`, com telefone e nascimento.
    signup_ticket: str | None = None
    name: str | None = None


class GoogleCompleteSignupRequest(BaseModel):
    """O que o Google nao da, e que `customers` exige.

    `phone` e `birth_date` sao NOT NULL na tabela e nao vem no `id_token`. O
    telefone em especial nao aceita sentinela: para cliente logado o
    `customer_phone_snapshot` do pedido sai de `customers.phone`, e um valor
    falso ali e o numero que o entregador liga.
    """

    signup_ticket: str
    phone: str = Field(min_length=8)
    birth_date: date
    privacy_accepted: bool
    marketing_opt_in: bool = False
    # O nome do Google, quando a pessoa quiser corrigir. Nulo mantem o do
    # ticket — que pode ser o proprio e-mail, se o perfil do Google nao tiver
    # nome.
    name: str | None = Field(default=None, min_length=1)

    @field_validator("phone")
    @classmethod
    def normalize_digits_value(cls, value: str) -> str:
        return normalize_digits(value)

