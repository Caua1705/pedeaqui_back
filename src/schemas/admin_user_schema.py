"""Cadastro de usuario do painel.

Tres decisoes deste arquivo que nao se leem no codigo:

**`role` e Literal, e nao `str` validado no service.** O Literal publica os
tres valores no `/openapi.json`, e e de la que o painel monta o seletor de
cargo. Se a lista viesse solta, a tela teria a segunda copia dela — e a copia
que envelhece e a que oferece "agente de impressao" como cargo de gente.

**`print_agent` tem recusa PROPRIA, antes do Literal.** O Literal ja recusaria,
com "Input should be 'owner', 'manager' or 'attendant'" — verdadeiro e inutil
para quem tentou. O validador `mode="before"` intercepta so esse valor e diz o
motivo, que e onde ele vai ser lido: no corpo da resposta 422.

**Nenhum schema daqui tem `password_hash`, e nenhum tem `restaurant_id` no
CORPO.** O primeiro porque o jeito rapido de montar o response e
`from_attributes=True` com os campos da tabela, e a coluna entraria junto. O
segundo porque o restaurante sai do token, sempre — aceita-lo no corpo criaria
o campo que a rota teria que desobedecer.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.constants import PAPEL_DE_MAQUINA
from src.utils.normalization import normalize_email


# Espelha `PAPEIS_DE_PESSOA`. As duas formas precisam existir — a tupla para o
# codigo, o Literal para o Pydantic e o OpenAPI — e
# `tests/test_admin_users.py::test_o_literal_espelha_a_constante` recusa que
# elas divirjam.
PapelDePessoa = Literal["owner", "manager", "attendant"]

RECUSA_DE_MAQUINA = (
    "print_agent e conta de maquina e nasce so pelo scripts/create_admin_user.py: "
    "criar um agente e parte de uma instalacao fisica, com alguem na loja "
    "editando o config.ini"
)


def _recusar_conta_de_maquina(value):
    if value == PAPEL_DE_MAQUINA:
        raise ValueError(RECUSA_DE_MAQUINA)
    return value


class AdminUserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=255)
    role: PapelDePessoa
    # Nulo = todas as filiais do restaurante. Para `owner` o campo nao muda
    # nada — `build_admin_scope` ignora a filial de quem e dono —, e por isso
    # ele nao e obrigatorio para papel nenhum.
    branch_id: uuid.UUID | None = None

    @field_validator("role", mode="before")
    @classmethod
    def recusar_conta_de_maquina(cls, value):
        return _recusar_conta_de_maquina(value)

    @field_validator("email")
    @classmethod
    def normalizar_email(cls, value: str) -> str:
        # O UNIQUE do banco e sobre `lower(email)` e e GLOBAL, nao por
        # restaurante. Normalizar aqui faz a checagem de duplicado do service
        # comparar a mesma coisa que o indice compara.
        return normalize_email(value)

    @field_validator("name")
    @classmethod
    def normalizar_nome(cls, value: str) -> str:
        nome = value.strip()
        if not nome:
            raise ValueError("name nao pode ser so espacos")
        return nome


class AdminUserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: PapelDePessoa | None = None
    branch_id: uuid.UUID | None = None
    is_active: bool | None = None

    @field_validator("role", mode="before")
    @classmethod
    def recusar_conta_de_maquina(cls, value):
        return _recusar_conta_de_maquina(value)

    @field_validator("name")
    @classmethod
    def normalizar_nome(cls, value: str | None) -> str | None:
        if value is None:
            return None
        nome = value.strip()
        if not nome:
            raise ValueError("name nao pode ser so espacos")
        return nome


class AdminUserDetailResponse(BaseModel):
    """O usuario como o painel o le. Nunca com `password_hash`.

    O e-mail SAI aqui: e a tela do dono sobre a propria equipe, e sem ele nao
    da para saber com qual conta a pessoa entra. Uma futura tela de gerente
    sobre a equipe da filial nao pode reusar este schema pelo mesmo motivo.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    restaurant_id: uuid.UUID
    branch_id: uuid.UUID | None
    name: str
    email: str
    role: str
    is_active: bool
    must_change_password: bool
    # Quando a pessoa trocou a senha pela ultima vez. Nulo = nunca trocou desde
    # a revisao 0013, que e o estado de todo usuario antigo — NAO significa
    # pendencia, e quem responde isso e `must_change_password`.
    password_changed_at: datetime | None = None
    created_at: datetime | None = None


class AdminUserCreatedResponse(BaseModel):
    """A UNICA vez que a senha temporaria existe fora do bcrypt.

    Nao ha rota que a devolva de novo, e isso e propriedade e nao limitacao:
    uma rota "me mostra de novo" seria uma rota que devolve a senha de outra
    pessoa. Segunda via e `POST /admin/users/{id}/reset-password`, que gera
    OUTRA e revoga os tokens da anterior.
    """

    admin_user: AdminUserDetailResponse
    temporary_password: str
