from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.repositories.menu_repository import MenuRepository
from src.repositories.product_repository import ProductRepository
from src.schemas.banner_schema import BannerResponse
from src.schemas.coupon_schema import CouponResponse
from src.schemas.menu_schema import RestaurantMenuResponse
from src.schemas.product_schema import ProductResponse
from src.schemas.restaurant_schema import BranchResponse, CategoryResponse, RestaurantSettingsResponse
from src.services.restaurant_service import RestaurantService
from src.utils.money import money_to_float
from src.utils.storage import build_storage_url


class MenuService:
    def __init__(self, db: Session):
        self.menu_repository = MenuRepository(db)
        self.product_repository = ProductRepository(db)
        self.restaurant_service = RestaurantService(db)

    def get_restaurant_menu(self, restaurant_slug: str) -> RestaurantMenuResponse:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        settings = self.menu_repository.get_settings(restaurant.id)

        return RestaurantMenuResponse(
            restaurant=self.restaurant_service.to_public_response(restaurant),
            settings=self._settings_response(settings) if settings else None,
            branches=[BranchResponse.model_validate(branch) for branch in self.menu_repository.get_active_branches(restaurant.id)],
            banners=[self._banner_response(banner) for banner in self.menu_repository.get_active_banners(restaurant.id)],
            coupons=[self._coupon_response(coupon) for coupon in self.menu_repository.get_active_coupons(restaurant.id)],
            categories=[CategoryResponse.model_validate(category) for category in self.menu_repository.get_active_categories(restaurant.id)],
            products=[self._product_response(product) for product in self.menu_repository.get_active_products(restaurant.id)],
            featured_products=[self._product_response(product) for product in self.menu_repository.get_featured_products(restaurant.id)],
        )

    def get_products_by_category(self, restaurant_slug: str, category_slug: str) -> list[ProductResponse]:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        if not self.product_repository.active_category_exists(restaurant.id, category_slug):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Categoria não encontrada")
        products = self.product_repository.list_active_by_category_slug(restaurant.id, category_slug)
        return [self._product_response(product) for product in products]

    def get_product_detail(self, restaurant_slug: str, product_slug: str) -> ProductResponse:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        product = self.product_repository.get_active_by_slug(restaurant.id, product_slug)
        if not product:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produto não encontrado")
        return self._product_response(product)

    @staticmethod
    def _settings_response(settings) -> RestaurantSettingsResponse:
        return RestaurantSettingsResponse(
            min_order_value=money_to_float(settings.min_order_value),
            estimated_delivery_time_min=settings.estimated_delivery_time_min,
            estimated_delivery_time_max=settings.estimated_delivery_time_max,
            default_delivery_fee=money_to_float(settings.default_delivery_fee),
            service_fee_enabled=settings.service_fee_enabled,
            service_fee_amount=money_to_float(settings.service_fee_amount),
            accepts_delivery=settings.accepts_delivery,
            accepts_pickup=settings.accepts_pickup,
            payment_methods=settings.payment_methods,
            is_open=settings.is_open,
        )

    @staticmethod
    def _product_response(product) -> ProductResponse:
        return ProductResponse(
            id=product.id,
            restaurant_id=product.restaurant_id,
            category_id=product.category_id,
            code=product.code,
            name=product.name,
            slug=product.slug,
            description=product.description,
            price=money_to_float(product.price),
            image_path=product.image_path,
            image_url=build_storage_url(product.image_path),
            is_active=product.is_active,
            is_available=product.is_available,
            sort_order=product.sort_order,
            is_featured=product.is_featured,
            badge=product.badge,
            highlight_order=product.highlight_order,
        )

    @staticmethod
    def _banner_response(banner) -> BannerResponse:
        return BannerResponse(
            id=banner.id,
            title=banner.title,
            subtitle=banner.subtitle,
            image_path=banner.image_path,
            image_url=build_storage_url(banner.image_path),
            action_type=banner.action_type,
            action_value=banner.action_value,
            sort_order=banner.sort_order,
            is_active=banner.is_active,
            starts_at=banner.starts_at,
            ends_at=banner.ends_at,
        )

    @staticmethod
    def _coupon_response(coupon) -> CouponResponse:
        return CouponResponse(
            id=coupon.id,
            code=coupon.code,
            title=coupon.title,
            description=coupon.description,
            discount_type=coupon.discount_type,
            discount_value=money_to_float(coupon.discount_value),
            min_order_value=money_to_float(coupon.min_order_value),
            image_path=coupon.image_path,
            image_url=build_storage_url(coupon.image_path),
            usage_limit=coupon.usage_limit,
            is_active=coupon.is_active,
            starts_at=coupon.starts_at,
            ends_at=coupon.ends_at,
        )
