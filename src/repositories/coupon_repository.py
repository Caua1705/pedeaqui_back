import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.models.coupon_redemption_model import CouponRedemption
from src.models.order_model import Order


class CouponRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id_and_restaurant(
        self,
        coupon_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> RestaurantCoupon | None:
        stmt = select(RestaurantCoupon).options(joinedload(RestaurantCoupon.template)).where(
            RestaurantCoupon.id == coupon_id,
            RestaurantCoupon.restaurant_id == restaurant_id,
        )
        if for_update:
            stmt = stmt.with_for_update(of=RestaurantCoupon)
        return self.db.scalar(stmt)

    def get_by_code_and_restaurant(
        self,
        code: str,
        restaurant_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> RestaurantCoupon | None:
        stmt = select(RestaurantCoupon).options(joinedload(RestaurantCoupon.template)).where(
            RestaurantCoupon.restaurant_id == restaurant_id,
            func.lower(RestaurantCoupon.code) == code.strip().lower(),
        )
        if for_update:
            stmt = stmt.with_for_update(of=RestaurantCoupon)
        return self.db.scalar(stmt)

    def lock_coupon(
        self,
        restaurant_id: uuid.UUID,
        *,
        coupon_id: uuid.UUID | None = None,
        coupon_code: str | None = None,
    ) -> RestaurantCoupon | None:
        if coupon_id is not None:
            return self.get_by_id_and_restaurant(coupon_id, restaurant_id, for_update=True)
        if coupon_code is not None:
            return self.get_by_code_and_restaurant(coupon_code, restaurant_id, for_update=True)
        return None

    def list_public_available(
        self,
        restaurant_id: uuid.UUID,
        now: datetime | None = None,
    ) -> list[RestaurantCoupon]:
        current = now or datetime.now(timezone.utc)
        stmt = (
            select(RestaurantCoupon)
            .options(joinedload(RestaurantCoupon.template))
            .where(
                RestaurantCoupon.restaurant_id == restaurant_id,
                RestaurantCoupon.is_public.is_(True),
                RestaurantCoupon.is_active.is_(True),
                RestaurantCoupon.valid_from <= current,
                RestaurantCoupon.valid_until >= current,
            )
            .order_by(RestaurantCoupon.sort_order.asc(), RestaurantCoupon.created_at.desc())
        )
        return list(self.db.scalars(stmt).unique().all())

    def list_by_restaurant(self, restaurant_id: uuid.UUID) -> list[RestaurantCoupon]:
        stmt = (
            select(RestaurantCoupon)
            .options(joinedload(RestaurantCoupon.template))
            .where(RestaurantCoupon.restaurant_id == restaurant_id)
            .order_by(RestaurantCoupon.created_at.desc())
        )
        return list(self.db.scalars(stmt).unique().all())

    def count_applied_total(self, coupon_id: uuid.UUID) -> int:
        stmt = select(func.count(CouponRedemption.id)).where(
            CouponRedemption.coupon_id == coupon_id,
            CouponRedemption.status == "applied",
        )
        return int(self.db.scalar(stmt) or 0)

    def count_applied_redemptions_for_customer(
        self,
        coupon_id: uuid.UUID,
        customer_id: uuid.UUID,
    ) -> int:
        stmt = select(func.count(CouponRedemption.id)).where(
            CouponRedemption.coupon_id == coupon_id,
            CouponRedemption.customer_id == customer_id,
            CouponRedemption.status == "applied",
        )
        return int(self.db.scalar(stmt) or 0)

    def get_last_applied_redemption_for_customer(
        self,
        coupon_id: uuid.UUID,
        customer_id: uuid.UUID,
    ) -> datetime | None:
        stmt = select(func.max(CouponRedemption.applied_at)).where(
            CouponRedemption.coupon_id == coupon_id,
            CouponRedemption.customer_id == customer_id,
            CouponRedemption.status == "applied",
        )
        return self.db.scalar(stmt)

    # Compatibility for callers created before the explicit repository name.
    def count_applied_by_customer(self, coupon_id: uuid.UUID, customer_id: uuid.UUID) -> int:
        return self.count_applied_redemptions_for_customer(coupon_id, customer_id)

    def customer_has_valid_order(self, customer_id: uuid.UUID, restaurant_id: uuid.UUID) -> bool:
        stmt = select(Order.id).where(
            Order.customer_id == customer_id,
            Order.restaurant_id == restaurant_id,
            Order.status.notin_(("cancelled", "rejected")),
        ).limit(1)
        return self.db.scalar(stmt) is not None

    def create_redemption(
        self,
        *,
        coupon_id: uuid.UUID,
        customer_id: uuid.UUID,
        order_id: uuid.UUID,
        discount_amount: Decimal,
        idempotency_key: str,
    ) -> CouponRedemption:
        redemption = CouponRedemption(
            coupon_id=coupon_id,
            customer_id=customer_id,
            order_id=order_id,
            discount_amount=discount_amount,
            status="applied",
            idempotency_key=idempotency_key,
        )
        self.db.add(redemption)
        self.db.flush()
        return redemption

    def get_redemption_by_order_id(self, order_id: uuid.UUID) -> CouponRedemption | None:
        stmt = select(CouponRedemption).where(CouponRedemption.order_id == order_id)
        return self.db.scalar(stmt)

    def reverse_redemption(self, redemption: CouponRedemption) -> CouponRedemption:
        if redemption.status != "applied":
            return redemption
        redemption.status = "reversed"
        redemption.reversed_at = datetime.now(timezone.utc)
        self.db.add(redemption)
        self.db.flush()
        return redemption

    def get_template(self, template_id: uuid.UUID) -> CouponTemplate | None:
        return self.db.get(CouponTemplate, template_id)

    def create_coupon(self, coupon: RestaurantCoupon) -> RestaurantCoupon:
        self.db.add(coupon)
        self.db.flush()
        return coupon

    def save_coupon(self, coupon: RestaurantCoupon) -> RestaurantCoupon:
        self.db.add(coupon)
        self.db.flush()
        return coupon
