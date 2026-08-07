"""Escopo de um lojista autenticado: restaurante e, quando houver, filial.

Existe para que a regra de filial seja resolvida em UM lugar. Antes cada
rota teria que lembrar de olhar `admin_user.branch_id` e cada esquecimento
seria um vazamento entre filiais — o mesmo tipo de erro que o
`restaurant_id` no path causava entre restaurantes.

A regra:

- role "owner": ve e edita todas as filiais do restaurante, mesmo que
  `admin_users.branch_id` esteja preenchido. Dono nao se prende a filial.
- role "manager" / "attendant": se `branch_id` estiver preenchido, so
  aquela filial; se for nulo, todas.

`branch_id = None` neste objeto significa SEMPRE "todas as filiais do
restaurante". O restaurante nunca e opcional: sai do token, ponto.
"""

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status

from src.api.dependencies.admin_auth import get_current_admin
from src.models.admin_user_model import AdminUser


# Papel que enxerga o restaurante inteiro. Deixado como constante para que a
# comparacao nao apareca solta como string no meio das regras.
UNRESTRICTED_ROLE = "owner"


@dataclass(frozen=True)
class AdminScope:
    """O que este lojista pode ver e escrever.

    Frozen de proposito: escopo e resultado da autenticacao, nao estado que
    um service possa afrouxar no meio de uma requisicao.
    """

    admin_user: AdminUser
    restaurant_id: uuid.UUID
    branch_id: uuid.UUID | None

    @property
    def sees_all_branches(self) -> bool:
        return self.branch_id is None

    def ensure_branch_allowed(self, branch_id: uuid.UUID) -> None:
        """Recusa uma filial fora do escopo.

        404 e nao 403 pelo mesmo motivo de `ensure_restaurant_scope`: um 403
        confirmaria que aquela filial existe. Para quem esta preso a uma
        filial, as outras simplesmente nao existem.
        """
        if self.branch_id is not None and self.branch_id != branch_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filial nao encontrada"
            )

    def resolve_branch_filter(self, requested_branch_id: uuid.UUID | None) -> uuid.UUID | None:
        """Filial a usar no WHERE de uma listagem.

        O filtro que o painel manda na querystring so restringe, nunca
        amplia: um lojista preso a uma filial que pedir outra recebe 404 em
        vez da lista alheia.
        """
        if requested_branch_id is None:
            return self.branch_id
        self.ensure_branch_allowed(requested_branch_id)
        return requested_branch_id


def build_admin_scope(admin_user: AdminUser) -> AdminScope:
    """Aplica a regra de filial a um lojista ja autenticado.

    Separada da dependencia porque o stream SSE nao autentica por
    `Depends(get_current_admin)` — ele valida um ticket de querystring — e
    precisa chegar exatamente no mesmo escopo. Duas copias da regra seriam
    duas chances de divergir.
    """
    branch_id = None if admin_user.role == UNRESTRICTED_ROLE else admin_user.branch_id
    return AdminScope(
        admin_user=admin_user,
        restaurant_id=admin_user.restaurant_id,
        branch_id=branch_id,
    )


def get_admin_scope(admin_user: AdminUser = Depends(get_current_admin)) -> AdminScope:
    return build_admin_scope(admin_user)
