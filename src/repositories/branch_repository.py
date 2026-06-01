import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.branch_model import Branch


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
            .order_by(Branch.is_main.desc(), Branch.name.asc())
        )
        return list(self.db.scalars(stmt).all())
