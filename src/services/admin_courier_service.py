"""O lado do PAINEL da frente de entregadores.

Tres assuntos, e os tres ficam no mesmo service porque partilham o mesmo
escopo (a filial do token) e o mesmo `_get_branch`: a taxa que a filial
paga, o cadastro dos motoboys, e a atribuicao de pedido a motoboy.

O que este arquivo NAO faz: escrever status de pedido. Quem escreve e
`OrderStatusChangeService`, e a porta do entregador e
`CourierDeliveryService`.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.models.branch_model import Branch
from src.repositories.branch_repository import BranchRepository
from src.schemas.courier_schema import (
    AdminBranchCourierFeeResponse,
    AdminBranchCourierFeeUpdate,
)
from src.utils.money import money_to_float, quantize_money


class AdminCourierService:
    def __init__(self, db: Session):
        self.db = db
        self.branch_repository = BranchRepository(db)

    # --- A taxa da filial ---------------------------------------------------

    def get_courier_fee(
        self, scope: AdminScope, branch_id: uuid.UUID
    ) -> AdminBranchCourierFeeResponse:
        branch = self._get_branch(scope, branch_id)
        return self._courier_fee_response(branch)

    def update_courier_fee(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: AdminBranchCourierFeeUpdate,
    ) -> AdminBranchCourierFeeResponse:
        """Grava a taxa desta filial. Campo ausente nao e tocado; `null`
        explicito apaga — e `exclude_unset` que separa os dois."""
        branch = self._get_branch(scope, branch_id)
        changes = payload.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(branch, field, None if value is None else quantize_money(value))
        self._commit()
        return self._courier_fee_response(branch)

    # --- Escopo -------------------------------------------------------------

    def _get_branch(self, scope: AdminScope, branch_id: uuid.UUID) -> Branch:
        """Filial dentro do escopo do token.

        Duas conferencias, e as duas sao necessarias (o padrao de
        `AdminSettingsService._get_branch`): `ensure_branch_allowed` barra a
        filial que existe mas nao e a deste lojista; o repositorio barra a
        filial de outro restaurante. Mesmo 404 para as duas.
        """
        scope.ensure_branch_allowed(branch_id)
        branch = self.branch_repository.get_active_by_id_and_restaurant(
            branch_id, scope.restaurant_id
        )
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filial não encontrada"
            )
        return branch

    def _commit(self) -> None:
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _courier_fee_response(branch: Branch) -> AdminBranchCourierFeeResponse:
        return AdminBranchCourierFeeResponse(
            branch_id=branch.id,
            courier_fee_base=(
                None if branch.courier_fee_base is None else money_to_float(branch.courier_fee_base)
            ),
            courier_fee_per_km=(
                None
                if branch.courier_fee_per_km is None
                else money_to_float(branch.courier_fee_per_km)
            ),
        )
