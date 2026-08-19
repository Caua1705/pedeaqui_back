from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.schemas.menu_schema import RestaurantMenuResponse
from src.schemas.product_schema import ProductResponse
from src.services.menu_service import MenuService


router = APIRouter(prefix="/restaurants", tags=["menu"])


@router.get("/{restaurant_slug}/menu", response_model=RestaurantMenuResponse)
def get_restaurant_menu(
    restaurant_slug: str,
    branch_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> RestaurantMenuResponse:
    """O cardapio, e a operacao da filial escolhida.

    Os PRODUTOS ainda sao do restaurante: `branch_id` nao filtra cardapio.
    Quem ele resolve e o bloco `settings` — valor minimo, taxa de servico,
    aceita entrega/retirada e o "fechar agora" sao da filial desde a revisao
    20260818_0025, e sem o parametro o cliente veria os numeros de uma loja
    enquanto pede em outra.

    Omitido, vale a filial padrao (principal se houver, senao a primeira
    ativa em ordem alfabetica) — a mesma de `POST /delivery/estimate` e de
    `GET /restaurants/{slug}/info` sem filial. Filial de outro restaurante
    responde 404.
    """
    return MenuService(db).get_restaurant_menu(restaurant_slug, branch_id)


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
