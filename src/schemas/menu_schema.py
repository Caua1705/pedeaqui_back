from pydantic import BaseModel

from src.schemas.banner_schema import BannerResponse
from src.schemas.coupon_schema import CouponResponse
from src.schemas.product_schema import ProductResponse
from src.schemas.restaurant_schema import (
    BranchResponse,
    CategoryResponse,
    RestaurantPublicResponse,
    RestaurantSettingsResponse,
)


class RestaurantMenuResponse(BaseModel):
    restaurant: RestaurantPublicResponse
    settings: RestaurantSettingsResponse | None
    branches: list[BranchResponse]
    banners: list[BannerResponse]
    coupons: list[CouponResponse]
    categories: list[CategoryResponse]
    products: list[ProductResponse]
    featured_products: list[ProductResponse]
