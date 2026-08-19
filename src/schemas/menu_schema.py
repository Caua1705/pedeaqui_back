from uuid import UUID

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

    # De qual filial o bloco `settings` esta falando. Vem do `branch_id` da
    # querystring quando ele e informado, e da filial padrao quando nao.
    #
    # Existe porque `settings` deixou de ser do restaurante: sem este campo,
    # o app nao teria como saber se o valor minimo que esta mostrando e o da
    # loja que o cliente escolheu. Nulo so quando o restaurante nao tem
    # filial ativa — e ai `settings` tambem vem nulo.
    settings_branch_id: UUID | None = None
    settings: RestaurantSettingsResponse | None
    branches: list[BranchResponse]
    banners: list[BannerResponse]
    highlight_banners: list[BannerResponse]
    coupons: list[PublicCouponResponse]
    categories: list[CategoryResponse]
    products: list[ProductResponse]
