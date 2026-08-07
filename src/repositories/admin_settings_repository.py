"""Consultas e escritas da configuracao do painel (BLOCO C da Fase 3).

O que ja existia em `BranchRepository` continua sendo usado de la (buscar
filial do restaurante, listar filiais ativas, listar as faixas de horario).
Aqui ficam so as consultas que o painel trouxe: as ESCRITAS de horario e de
forma de pagamento, e a leitura das formas desabilitadas — o cardapio
publico so pede as habilitadas.

Forma de pagamento nao tem `restaurant_id`; a juncao com `branches` e o que
impede editar a linha de outro restaurante.
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_model import Branch
from src.models.branch_payment_method_model import BranchPaymentMethod
from src.models.restaurant_setting_model import RestaurantSetting


class AdminSettingsRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_settings(self, restaurant_id: uuid.UUID) -> RestaurantSetting | None:
        stmt = select(RestaurantSetting).where(
            RestaurantSetting.restaurant_id == restaurant_id
        )
        return self.db.scalar(stmt)

    def add_settings(self, settings: RestaurantSetting) -> RestaurantSetting:
        self.db.add(settings)
        self.db.flush()
        return settings

    def delete_business_hours(self, branch_id: uuid.UUID) -> None:
        self.db.execute(
            delete(BranchBusinessHour).where(BranchBusinessHour.branch_id == branch_id)
        )

    def add_business_hours(self, periods: list[BranchBusinessHour]) -> list[BranchBusinessHour]:
        self.db.add_all(periods)
        self.db.flush()
        return periods

    def list_payment_methods(self, branch_id: uuid.UUID) -> list[BranchPaymentMethod]:
        # Sem filtro por `enabled`: o painel precisa ver a forma desligada
        # para religa-la.
        stmt = (
            select(BranchPaymentMethod)
            .where(BranchPaymentMethod.branch_id == branch_id)
            .order_by(
                BranchPaymentMethod.payment_flow.asc(),
                BranchPaymentMethod.sort_order.asc(),
                BranchPaymentMethod.id.asc(),
            )
        )
        return list(self.db.scalars(stmt).all())

    def get_payment_method(
        self,
        method_id: uuid.UUID,
        restaurant_id: uuid.UUID,
    ) -> BranchPaymentMethod | None:
        stmt = (
            select(BranchPaymentMethod)
            .join(Branch, Branch.id == BranchPaymentMethod.branch_id)
            .where(
                BranchPaymentMethod.id == method_id,
                Branch.restaurant_id == restaurant_id,
            )
        )
        return self.db.scalar(stmt)

    def get_payment_method_by_type(
        self,
        branch_id: uuid.UUID,
        payment_flow: str,
        method_type: str,
        brand: str | None,
    ) -> BranchPaymentMethod | None:
        stmt = select(BranchPaymentMethod).where(
            BranchPaymentMethod.branch_id == branch_id,
            BranchPaymentMethod.payment_flow == payment_flow,
            BranchPaymentMethod.method_type == method_type,
            BranchPaymentMethod.brand.is_(None) if brand is None
            else BranchPaymentMethod.brand == brand,
        )
        return self.db.scalar(stmt)

    def add_payment_method(self, method: BranchPaymentMethod) -> BranchPaymentMethod:
        self.db.add(method)
        self.db.flush()
        return method

    def delete_payment_method(self, method: BranchPaymentMethod) -> None:
        self.db.delete(method)
