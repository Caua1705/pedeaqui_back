import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.restaurant_model import Restaurant


class RestaurantRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_active_by_slug(self, slug: str) -> Restaurant | None:
        stmt = select(Restaurant).where(Restaurant.slug == slug, Restaurant.is_active.is_(True))
        return self.db.scalar(stmt)

    def get_by_id(self, restaurant_id: uuid.UUID) -> Restaurant | None:
        return self.db.get(Restaurant, restaurant_id)

    def get_active_by_id(self, restaurant_id: uuid.UUID) -> Restaurant | None:
        stmt = select(Restaurant).where(
            Restaurant.id == restaurant_id,
            Restaurant.is_active.is_(True),
        )
        return self.db.scalar(stmt)
