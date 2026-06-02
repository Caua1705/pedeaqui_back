from pydantic import BaseModel

from src.schemas.banner_schema import BannerResponse
from src.schemas.coupon_schema import PublicCouponResponse
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
    highlight_banners: list[BannerResponse]
    coupons: list[PublicCouponResponse]
    categories: list[CategoryResponse]
    products: list[ProductResponse]
