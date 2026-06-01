import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.models.restaurant_banner_model import RestaurantBanner


class BannerRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_active_by_restaurant(self, restaurant_id: uuid.UUID) -> list[RestaurantBanner]:
        now = datetime.now(timezone.utc)
        stmt = (
            select(RestaurantBanner)
            .where(
                RestaurantBanner.restaurant_id == restaurant_id,
                RestaurantBanner.is_active.is_(True),
                or_(RestaurantBanner.starts_at.is_(None), RestaurantBanner.starts_at <= now),
                or_(RestaurantBanner.ends_at.is_(None), RestaurantBanner.ends_at >= now),
            )
            .order_by(RestaurantBanner.sort_order.asc())
        )
        return list(self.db.scalars(stmt).all())
