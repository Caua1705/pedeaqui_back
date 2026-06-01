from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.schemas.menu_schema import RestaurantMenuResponse
from src.schemas.product_schema import ProductResponse
from src.services.menu_service import MenuService


router = APIRouter(prefix="/restaurants", tags=["menu"])


@router.get("/{restaurant_slug}/menu", response_model=RestaurantMenuResponse)
def get_restaurant_menu(
    restaurant_slug: str,
    db: Session = Depends(get_db),
) -> RestaurantMenuResponse:
    return MenuService(db).get_restaurant_menu(restaurant_slug)


@router.get("/{restaurant_slug}/categories/{category_slug}/products", response_model=list[ProductResponse])
def get_products_by_category(
    restaurant_slug: str,
    category_slug: str,
    db: Session = Depends(get_db),
) -> list[ProductResponse]:
    return MenuService(db).get_products_by_category(restaurant_slug, category_slug)


@router.get("/{restaurant_slug}/products/{product_slug}", response_model=ProductResponse)
def get_product_detail(
    restaurant_slug: str,
    product_slug: str,
    db: Session = Depends(get_db),
) -> ProductResponse:
    return MenuService(db).get_product_detail(restaurant_slug, product_slug)
