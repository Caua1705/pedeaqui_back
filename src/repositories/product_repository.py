import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.models.category_model import Category
from src.models.product_model import Product
from src.models.product_option_model import ProductOptionGroup


class ProductRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_active_by_category_slug(self, restaurant_id: uuid.UUID, category_slug: str) -> list[Product]:
        stmt = (
            select(Product)
            .join(Category, Product.category_id == Category.id)
            .options(selectinload(Product.option_groups).selectinload(ProductOptionGroup.options))
            .where(
                Product.restaurant_id == restaurant_id,
                Category.restaurant_id == restaurant_id,
                Category.slug == category_slug,
                Category.is_active.is_(True),
                Product.is_active.is_(True),
                Product.is_available.is_(True),
            )
            .order_by(Product.sort_order.asc(), Product.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def active_category_exists(self, restaurant_id: uuid.UUID, category_slug: str) -> bool:
        stmt = select(Category.id).where(
            Category.restaurant_id == restaurant_id,
            Category.slug == category_slug,
            Category.is_active.is_(True),
        )
        return self.db.scalar(stmt) is not None

    def get_active_by_slug(self, restaurant_id: uuid.UUID, product_slug: str) -> Product | None:
        stmt = (
            select(Product)
            .join(Category, Product.category_id == Category.id)
            .options(selectinload(Product.option_groups).selectinload(ProductOptionGroup.options))
            .where(
                Product.restaurant_id == restaurant_id,
                Product.slug == product_slug,
                Product.is_active.is_(True),
                Product.is_available.is_(True),
                Category.is_active.is_(True),
            )
        )
        return self.db.scalar(stmt)

    def list_active_by_ids(self, restaurant_id: uuid.UUID, product_ids: list[uuid.UUID]) -> list[Product]:
        stmt = (
            select(Product)
            .options(selectinload(Product.option_groups).selectinload(ProductOptionGroup.options))
            .where(
                Product.restaurant_id == restaurant_id,
                Product.id.in_(product_ids),
                Product.is_active.is_(True),
                Product.is_available.is_(True),
            )
        )
        return list(self.db.scalars(stmt).all())
