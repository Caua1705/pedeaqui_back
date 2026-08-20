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
    """O cardapio de UMA filial. Ver `MenuService.get_restaurant_menu`."""

    restaurant: RestaurantPublicResponse

    # De qual filial esta resposta INTEIRA esta falando: produtos,
    # categorias, precos, disponibilidade e o bloco `settings`. Vem do
    # `branch_id` da querystring quando ele e informado, e da filial padrao
    # quando nao.
    #
    # Nulo so quando o restaurante nao tem filial ativa — e ai `settings` vem
    # nulo e as listas de produto e categoria vem vazias.
    branch_id: UUID | None = None

    # OBSOLETO desde a revisao 20260820_0026: mesmo valor de `branch_id`.
    #
    # Nasceu na 20260818_0025, quando so o bloco `settings` era da filial e o
    # cardapio ainda era do restaurante — o nome descrevia a verdade daquele
    # dia. Hoje ele mente por omissao: quem le "settings_branch_id" nao
    # imagina que os PRODUTOS tambem sao dessa filial.
    #
    # Fica pelo tempo de o painel e o app trocarem de campo. Renomear no
    # lugar quebraria os dois de uma vez (armadilha 16); campo novo com
    # default e de graca (armadilha 7).
    settings_branch_id: UUID | None = None
    settings: RestaurantSettingsResponse | None
    branches: list[BranchResponse]
    banners: list[BannerResponse]
    highlight_banners: list[BannerResponse]
    coupons: list[PublicCouponResponse]
    categories: list[CategoryResponse]
    products: list[ProductResponse]
