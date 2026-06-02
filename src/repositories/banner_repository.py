import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.restaurant_banner_model import RestaurantBanner


class BannerRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_active_by_restaurant(self, restaurant_id: uuid.UUID) -> list[RestaurantBanner]:
        return self.get_banners_by_type(restaurant_id, "hero")

    def get_banners_by_type(self, restaurant_id: uuid.UUID, banner_type: str) -> list[RestaurantBanner]:
        stmt = (
            select(RestaurantBanner)
            .where(
                RestaurantBanner.restaurant_id == restaurant_id,
                RestaurantBanner.banner_type == banner_type,
                RestaurantBanner.is_active.is_(True),
            )
            .order_by(RestaurantBanner.sort_order.asc())
        )
        return list(self.db.scalars(stmt).all())
