import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.repositories.menu_repository import MenuRepository
from src.repositories.product_repository import ProductRepository
from src.schemas.banner_schema import BannerResponse
from src.schemas.coupon_schema import PublicCouponResponse
from src.schemas.menu_schema import RestaurantMenuResponse
from src.schemas.product_schema import ProductOptionGroupResponse, ProductOptionResponse, ProductResponse
from src.schemas.restaurant_schema import BranchResponse, CategoryResponse, RestaurantSettingsResponse
from src.services.menu_rules import blocking_required_group
from src.services.restaurant_service import RestaurantService
from src.utils.money import money_to_float
from src.utils.storage import build_storage_url


logger = logging.getLogger("uvicorn.error")


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
            banners=[self._banner_response(banner) for banner in self.menu_repository.get_banners_by_type(restaurant.id, "hero")],
            highlight_banners=[
                self._banner_response(banner)
                for banner in self.menu_repository.get_banners_by_type(restaurant.id, "highlight")
            ],
            coupons=self._coupon_responses(restaurant.id),
            categories=[CategoryResponse.model_validate(category) for category in self.menu_repository.get_active_categories(restaurant.id)],
            products=self._sellable_product_responses(self.menu_repository.get_active_products(restaurant.id)),
        )

    def get_products_by_category(self, restaurant_slug: str, category_slug: str) -> list[ProductResponse]:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        if not self.product_repository.active_category_exists(restaurant.id, category_slug):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Categoria não encontrada")
        products = self.product_repository.list_active_by_category_slug(restaurant.id, category_slug)
        return self._sellable_product_responses(products)

    def get_product_detail(self, restaurant_slug: str, product_slug: str) -> ProductResponse:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        product = self.product_repository.get_active_by_slug(restaurant.id, product_slug)
        if not product:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produto não encontrado")

        # Fora de venda responde 404 igual a produto inexistente, e nao uma
        # mensagem propria: o link do produto e publico e compartilhavel, e
        # distinguir os dois casos aqui contaria a quem tem o link o que esta
        # acontecendo dentro da loja. O lojista descobre pelo /admin.
        blocking = blocking_required_group(product)
        if blocking is not None:
            logger.warning(
                "[Cardapio] produto fora de venda: grupo obrigatorio sem opcao ativa "
                "| product_id=%s | produto=%s | option_group_id=%s | grupo=%s",
                product.id,
                product.name,
                blocking.id,
                blocking.name,
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produto não encontrado")

        return self.product_response(product)

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
    def product_response(product) -> ProductResponse:
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
            option_groups=[
                ProductOptionGroupResponse(
                    id=group.id,
                    name=group.name,
                    description=group.description,
                    min_select=group.min_select,
                    max_select=group.max_select,
                    is_required=group.is_required,
                    sort_order=group.sort_order,
                    options=[
                        ProductOptionResponse(
                            id=option.id,
                            name=option.name,
                            description=option.description,
                            additional_price=money_to_float(option.additional_price),
                            sort_order=option.sort_order,
                        )
                        for option in sorted(
                            (option for option in group.options if option.is_active),
                            key=lambda option: (option.sort_order or 0, option.name),
                        )
                    ],
                )
                for group in sorted(
                    MenuService._answerable_option_groups(product),
                    key=lambda group: (group.sort_order or 0, group.name),
                )
            ],
        )

    @staticmethod
    def _answerable_option_groups(product) -> list:
        """Os grupos que o cliente CONSEGUE responder.

        Grupo sem nenhuma opcao ativa nao oferece escolha nenhuma, entao nao
        aparece. Isto aqui e cosmetico e vale so para o grupo OPCIONAL: quando
        o grupo vazio e obrigatorio, o produto inteiro sai de venda antes de
        chegar aqui (`services/menu_rules.blocking_required_group`).
        """
        return [
            group
            for group in product.option_groups
            if group.is_active and any(option.is_active for option in group.options)
        ]

    def _sellable_product_responses(self, products) -> list[ProductResponse]:
        """A lista do cardapio, sem os produtos que nao tem como ser vendidos."""
        responses = []
        for product in products:
            blocking = blocking_required_group(product)
            if blocking is not None:
                logger.warning(
                    "[Cardapio] produto fora de venda: grupo obrigatorio sem opcao ativa "
                    "| product_id=%s | produto=%s | option_group_id=%s | grupo=%s",
                    product.id,
                    product.name,
                    blocking.id,
                    blocking.name,
                )
                continue
            responses.append(self.product_response(product))
        return responses

    @staticmethod
    def _banner_response(banner) -> BannerResponse:
        return BannerResponse(
            id=banner.id,
            restaurant_id=banner.restaurant_id,
            banner_type=banner.banner_type,
            image_path=banner.image_path,
            image_url=build_storage_url(banner.image_path),
            sort_order=banner.sort_order,
            is_active=banner.is_active,
        )

    def _coupon_responses(self, restaurant_id) -> list[PublicCouponResponse]:
        """Os cupons da vitrine — sem os que nao tem como ser montados.

        UM CUPOM QUEBRADO NAO DERRUBA O CARDAPIO. `_coupon_response` le
        `coupon.template.image_path`, e um cupom ativo cujo template foi
        apagado levantava AttributeError bem no meio da comprehension que
        montava a lista — ou seja, a vitrine INTEIRA respondia 500. O cliente
        nao via "cupom indisponivel", via a loja fora do ar, e o lojista nao
        tinha como adivinhar que a causa era um cupom.

        O template ausente e sempre defeito de dado (a FK deveria impedir),
        entao ele e REGISTRADO no log em vez de ignorado em silencio: sem
        isso, o cupom que o lojista criou simplesmente nao apareceria e nao
        haveria onde procurar o porque.
        """
        responses = []
        for coupon in self.menu_repository.get_active_coupons(restaurant_id):
            if coupon.template is None:
                logger.warning(
                    "[Cardapio] cupom ativo sem template, fora da vitrine | coupon_id=%s | code=%s",
                    coupon.id,
                    coupon.code,
                )
                continue
            responses.append(self._coupon_response(coupon))
        return responses

    @staticmethod
    def _coupon_response(coupon) -> PublicCouponResponse:
        template = coupon.template
        return PublicCouponResponse(
            id=coupon.id,
            code=coupon.code,
            name=coupon.title,
            image_path=template.image_path,
            image_url=build_storage_url(template.image_path),
            discount_type=coupon.discount_type,
            discount_value=money_to_float(coupon.discount_value),
            min_order_value=money_to_float(coupon.min_order_value),
            sort_order=coupon.sort_order or 0,
            is_active=bool(coupon.is_active),
        )
