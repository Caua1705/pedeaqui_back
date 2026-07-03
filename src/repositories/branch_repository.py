import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.branch_model import Branch
from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_payment_method_model import BranchPaymentMethod


class BranchRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_active_by_id_and_restaurant(self, branch_id: uuid.UUID, restaurant_id: uuid.UUID) -> Branch | None:
        stmt = select(Branch).where(
            Branch.id == branch_id,
            Branch.restaurant_id == restaurant_id,
            Branch.is_active.is_(True),
        )
        return self.db.scalar(stmt)

    def list_active_by_restaurant(self, restaurant_id: uuid.UUID) -> list[Branch]:
        stmt = (
            select(Branch)
            .where(Branch.restaurant_id == restaurant_id, Branch.is_active.is_(True))
            .order_by(Branch.is_main.desc().nulls_last(), Branch.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def list_business_hours(self, branch_id: uuid.UUID) -> list[BranchBusinessHour]:
        stmt = (
            select(BranchBusinessHour)
            .where(BranchBusinessHour.branch_id == branch_id)
            .order_by(
                BranchBusinessHour.weekday.asc(),
                BranchBusinessHour.sort_order.asc(),
                BranchBusinessHour.id.asc(),
            )
        )
        return list(self.db.scalars(stmt).all())

    def list_enabled_payment_methods(
        self, branch_id: uuid.UUID
    ) -> list[BranchPaymentMethod]:
        stmt = (
            select(BranchPaymentMethod)
            .where(
                BranchPaymentMethod.branch_id == branch_id,
                BranchPaymentMethod.enabled.is_(True),
            )
            .order_by(
                BranchPaymentMethod.payment_flow.asc(),
                BranchPaymentMethod.sort_order.asc(),
                BranchPaymentMethod.id.asc(),
            )
        )
        return list(self.db.scalars(stmt).all())
