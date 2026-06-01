import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.models.coupon_model import Coupon


class CouponRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_active_by_restaurant(self, restaurant_id: uuid.UUID) -> list[Coupon]:
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
