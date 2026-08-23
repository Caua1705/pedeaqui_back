import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    SOMENTE_DONO,
    AdminScope,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.database import get_db
from src.schemas.admin_user_schema import (
    AdminUserCreate,
    AdminUserCreatedResponse,
    AdminUserDetailResponse,
    AdminUserUpdate,
)
from src.services.admin_user_service import AdminUserService


# **As quatro sao SOMENTE_DONO, inclusive a de LER.**
#
# A lista diz quem tem acesso ao faturamento do restaurante: e o mapa de quem
# atacar, e nao e informacao que o balcao precise para atender. Foi decidido
# junto com a pergunta que faltava — "o gerente precisa ver a equipe da
# filial?" —, e a resposta foi nao: com um restaurante de duas filiais, quem
# cadastra e o dono.
#
# Se um dia virar necessidade, e ROTA DIFERENTE, com outro recorte: filial em
# vez de restaurante, e sem e-mail na resposta. Afrouxar o papel destas aqui
# entregaria o restaurante inteiro para resolver uma pergunta sobre uma loja.
#
# **Nao ha DELETE, e a ausencia e deliberada** — o motivo esta no docstring do
# `AdminUserService`. Desativar e `PATCH {"is_active": false}`, e vale na
# requisicao seguinte.
router = APIRouter(prefix="/admin/users", tags=["admin users"])


@router.get(
    "",
    response_model=list[AdminUserDetailResponse],
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def list_admin_users(
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[AdminUserDetailResponse]:
    """A equipe do restaurante. A conta de maquina fica de fora.

    O agente de impressao ja tem tela propria em `/admin/printing`, que e onde
    ele faz sentido — com nome de impressora e heartbeat ao lado. Aqui ele
    seria uma linha sem cargo e sem ninguem por tras.
    """
    return AdminUserService(db).list_people(scope)


@router.post(
    "",
    response_model=AdminUserCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def create_admin_user(
    payload: AdminUserCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminUserCreatedResponse:
    """Cadastra alguem e devolve a senha temporaria UMA vez.

    A senha vem no corpo da resposta e nao existe em nenhum outro lugar: o
    banco so tem o bcrypt dela. A tela precisa mostra-la com botao de copiar e
    deixar claro que ela nao volta — segunda via e
    `POST /admin/users/{id}/reset-password`, que gera outra.

    `role` nao aceita `print_agent`: conta de maquina nasce so pelo
    `scripts/create_admin_user.py`, porque criar um agente e parte de uma
    instalacao fisica.
    """
    return AdminUserService(db).create(scope, payload)


@router.patch(
    "/{admin_user_id}",
    response_model=AdminUserDetailResponse,
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def update_admin_user(
    admin_user_id: uuid.UUID,
    payload: AdminUserUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminUserDetailResponse:
    """Nome, papel, filial e `is_active`. Desativar tem efeito imediato.

    `is_active: false` vale na requisicao SEGUINTE daquela pessoa, sem esperar
    as 12h do token: `_load_admin_from_token` recarrega o usuario do banco a
    cada chamada. Vale tambem para a conexao SSE, na proxima reconexao.

    Tres coisas a rota recusa com 400, e as tres sao o mesmo buraco: desativar
    a propria conta, desativar o unico dono ativo, rebaixar o unico dono ativo.
    """
    return AdminUserService(db).update(scope, admin_user_id, payload)


@router.post(
    "/{admin_user_id}/reset-password",
    response_model=AdminUserCreatedResponse,
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def reset_admin_user_password(
    admin_user_id: uuid.UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminUserCreatedResponse:
    """Nova senha temporaria, e todo token daquela pessoa morre na hora.

    E para quem perdeu a senha e para a suspeita de vazamento. Se a pessoa for
    o usuario de um agente de impressao... nao e: `print_agent` nao e alcancado
    por estas rotas. A rotacao daquela senha continua sendo
    `scripts/create_admin_user.py --reset-password`, seguida de editar o
    `config.ini` da maquina do balcao.
    """
    return AdminUserService(db).reset_password(scope, admin_user_id)
