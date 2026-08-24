from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    """A resposta de `GET /health`.

    `git_sha` e o commit de que a imagem foi construida, e existe aqui para a
    pergunta "qual versao esta no ar AGORA?" ter resposta sem precisar de
    acesso ao log do container. Vale `nao-carimbado` quando a imagem foi
    construida sem o build arg — ver `Settings.GIT_SHA`.

    **Nao ha risco de vazamento aqui.** O repositorio e privado, e um hash de
    commit nao diz nada a quem nao o tem; o que ele evita e uma investigacao
    inteira. A rota ja era publica e ja dizia o nome da aplicacao.
    """

    status: str
    app: str
    git_sha: str


class StatusHistoryResponse(BaseResponse):
    id: UUID
    status: str
    changed_by: str | None = None
    note: str | None = None
    created_at: datetime | None = None
