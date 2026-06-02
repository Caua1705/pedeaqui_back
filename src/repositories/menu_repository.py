import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.models.branch_model import Branch
from src.models.category_model import Category
from src.models.coupon_model import Coupon
from src.models.product_model import Product
from src.models.restaurant_banner_model import RestaurantBanner
from src.models.restaurant_setting_model import RestaurantSetting


class MenuRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_settings(self, restaurant_id: uuid.UUID) -> RestaurantSetting | None:
        stmt = select(RestaurantSetting).where(RestaurantSetting.restaurant_id == restaurant_id)
        return self.db.scalar(stmt)

    def get_active_branches(self, restaurant_id: uuid.UUID) -> list[Branch]:
        stmt = (
            select(Branch)
            .where(Branch.restaurant_id == restaurant_id, Branch.is_active.is_(True))
            .order_by(Branch.is_main.desc(), Branch.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_active_categories(self, restaurant_id: uuid.UUID) -> list[Category]:
        stmt = (
            select(Category)
            .where(Category.restaurant_id == restaurant_id, Category.is_active.is_(True))
            .order_by(Category.sort_order.asc(), Category.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_active_products(self, restaurant_id: uuid.UUID) -> list[Product]:
        stmt = (
            select(Product)
            .join(Category, Product.category_id == Category.id)
            .where(
                Product.restaurant_id == restaurant_id,
                Product.is_active.is_(True),
                Product.is_available.is_(True),
                Category.is_active.is_(True),
            )
            .order_by(Category.sort_order.asc(), Product.sort_order.asc(), Product.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_banners_by_type(self, restaurant_id: uuid.UUID, banner_type: str) -> list[RestaurantBanner]:
        now = datetime.now(timezone.utc)
        stmt = (
            select(RestaurantBanner)
            .where(
                RestaurantBanner.restaurant_id == restaurant_id,
                RestaurantBanner.banner_type == banner_type,
                RestaurantBanner.is_active.is_(True),
                or_(RestaurantBanner.starts_at.is_(None), RestaurantBanner.starts_at <= now),
                or_(RestaurantBanner.ends_at.is_(None), RestaurantBanner.ends_at >= now),
            )
            .order_by(RestaurantBanner.sort_order.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_active_coupons(self, restaurant_id: uuid.UUID) -> list[Coupon]:
        now = datetime.now(timezone.utc)
        stmt = (
            select(Coupon)
            .where(
                Coupon.restaurant_id == restaurant_id,
                Coupon.is_active.is_(True),
                or_(Coupon.starts_at.is_(None), Coupon.starts_at <= now),
                or_(Coupon.ends_at.is_(None), Coupon.ends_at >= now),
            )
            .order_by(Coupon.created_at.desc())
        )
        return list(self.db.scalars(stmt).all())
