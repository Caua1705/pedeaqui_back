import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from src.models.branch_model import Branch
from src.models.category_model import Category
from src.models.coupon_model import COUPON_VISIBILITY_PUBLIC, CouponTemplate, RestaurantCoupon
from src.models.product_model import Product
from src.models.product_option_model import ProductOptionGroup
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

    def get_active_categories(self, branch_id: uuid.UUID) -> list[Category]:
        """As categorias DAQUELA loja.

        Filtra por filial e nao por restaurante desde a revisao
        20260820_0026: cardapio e da filial, sem heranca. Filtrar por
        restaurante devolveria as categorias das duas lojas na mesma lista,
        e o cliente veria secao vazia para a que nao e dele.
        """
        stmt = (
            select(Category)
            .where(Category.branch_id == branch_id, Category.is_active.is_(True))
            .order_by(Category.sort_order.asc(), Category.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_active_products(self, branch_id: uuid.UUID) -> list[Product]:
        """Os produtos vendaveis DAQUELA loja.

        `Category.branch_id` no WHERE alem de `Product.branch_id` e
        redundante — a FK composta (branch_id, category_id) garante que os
        dois concordam — e fica de fora: consulta que repete o que o banco ja
        garante so envelhece.
        """
        stmt = (
            select(Product)
            .join(Category, Product.category_id == Category.id)
            .options(selectinload(Product.option_groups).selectinload(ProductOptionGroup.options))
            .where(
                Product.branch_id == branch_id,
                Product.is_active.is_(True),
                Product.is_available.is_(True),
                Category.is_active.is_(True),
            )
            .order_by(Category.sort_order.asc(), Product.sort_order.asc(), Product.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_banners_by_type(self, restaurant_id: uuid.UUID, banner_type: str) -> list[RestaurantBanner]:
        stmt = (
            select(RestaurantBanner)
            .where(
                RestaurantBanner.restaurant_id == restaurant_id,
                RestaurantBanner.banner_type == banner_type,
                RestaurantBanner.is_active.is_(True),
            )
            .order_by(RestaurantBanner.sort_order.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_active_coupons(self, restaurant_id: uuid.UUID) -> list[RestaurantCoupon]:
        """A vitrine do cardapio: SO cupom `public`, e so ele.

        Esta consulta e a unica superficie de cupom que nao passa por
        `CouponService.evaluate` — ela nao tem cliente para avaliar contra
        (`GET /{slug}/menu` e anonima) e existe para o card decorativo do
        cardapio, nao para dizer se o cupom serve.

        **Por isso o filtro tem que ser `= 'public'`, e nunca
        `!= 'private'`.** A segunda forma parece equivalente e publica os
        cupons de SEGMENTO para todo mundo — a campanha "para quem sumiu"
        aparecendo, com codigo, na vitrine que qualquer pessoa abre sem
        login. Nao daria erro, nao daria log, e o sintoma seria o lojista
        pagando desconto de reativacao para quem pede toda semana.

        Cupom privado e cupom de segmento aparecem em `GET /{slug}/coupons`,
        que tem token e roda o gate.
        """
        stmt = (
            select(RestaurantCoupon)
            .join(CouponTemplate, CouponTemplate.id == RestaurantCoupon.coupon_template_id)
            .options(joinedload(RestaurantCoupon.template))
            .where(
                RestaurantCoupon.restaurant_id == restaurant_id,
                RestaurantCoupon.is_active.is_(True),
                RestaurantCoupon.visibility == COUPON_VISIBILITY_PUBLIC,
                RestaurantCoupon.valid_from <= datetime.now(timezone.utc),
                RestaurantCoupon.valid_until >= datetime.now(timezone.utc),
                CouponTemplate.is_active.is_(True),
            )
            .order_by(RestaurantCoupon.sort_order.asc())
        )
        return list(self.db.scalars(stmt).all())
