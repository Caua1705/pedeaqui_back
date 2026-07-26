import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.admin_user_model import AdminUser


class AdminUserRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_email(self, email: str) -> AdminUser | None:
        # lower() dos dois lados para casar com o indice unico funcional
        # criado na migracao 20260726_0003.
        stmt = select(AdminUser).where(func.lower(AdminUser.email) == email.lower())
        return self.db.scalar(stmt)

    def get_by_id(self, admin_user_id: uuid.UUID) -> AdminUser | None:
        return self.db.get(AdminUser, admin_user_id)

    def list_by_restaurant(self, restaurant_id: uuid.UUID) -> list[AdminUser]:
        stmt = (
            select(AdminUser)
            .where(AdminUser.restaurant_id == restaurant_id)
            .order_by(AdminUser.created_at)
        )
        return list(self.db.scalars(stmt).all())

    def create(self, admin_user: AdminUser) -> AdminUser:
        self.db.add(admin_user)
        self.db.flush()
        return admin_user
