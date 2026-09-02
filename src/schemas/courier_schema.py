"""Entregadores: o contrato do painel (`/admin/...`) e o do entregador
(`/courier/...`), no mesmo arquivo porque os dois falam do mesmo cadastro.

Dinheiro sai como `float` via `money_to_float`, como o resto do painel
(armadilha 34: nenhuma resposta nova entra na excecao dos dois schemas de
pedido).
"""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from src.schemas.common_schema import BaseResponse


class AdminBranchCourierFeeUpdate(BaseModel):
    """A taxa do entregador DESTA filial. Dois estados por campo:

    - campo ausente do corpo: nao mexe;
    - campo com valor, ou com `null` explicito: grava. `null` e "sem taxa"
      (a atribuicao passa a congelar snapshot nulo), e nao "volta a herdar"
      — aqui nao ha heranca, como nas cinco colunas do frete do cliente.
    """

    courier_fee_base: Decimal | None = Field(default=None, ge=0)
    courier_fee_per_km: Decimal | None = Field(default=None, ge=0)


class AdminBranchCourierFeeResponse(BaseResponse):
    branch_id: UUID
    courier_fee_base: float | None = None
    courier_fee_per_km: float | None = None
