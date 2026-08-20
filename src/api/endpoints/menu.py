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
    """O cardapio DAQUELA FILIAL.

    Desde a revisao 20260820_0026 o `branch_id` resolve a resposta inteira:
    produtos, categorias, precos, disponibilidade e o bloco `settings`. Cada
    loja tem o proprio cardapio, sem heranca — chamar sem o parametro depois
    de o cliente ter escolhido a loja mostra o cardapio e os numeros de
    outra.

    Omitido, vale a filial padrao (principal se houver, senao a primeira
    ativa em ordem alfabetica) — a mesma de `POST /delivery/estimate` e de
    `GET /restaurants/{slug}/info` sem filial. Filial de outro restaurante
    responde 404; restaurante sem filial ativa responde 200 com as listas
    vazias.
    """
    return MenuService(db).get_restaurant_menu(restaurant_slug, branch_id)


@router.get("/{restaurant_slug}/categories/{category_slug}/products", response_model=list[ProductResponse])
def get_products_by_category(
    restaurant_slug: str,
    category_slug: str,
    branch_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[ProductResponse]:
    """Os produtos de uma categoria, dentro de uma filial.

    O `category_slug` e unico por `(branch_id, slug)`, entao sem o parametro
    esta rota responde pela filial padrao. Categoria que so existe em outra
    loja responde 404.
    """
    return MenuService(db).get_products_by_category(restaurant_slug, category_slug, branch_id)


@router.get("/{restaurant_slug}/products/{product_slug}", response_model=ProductResponse)
def get_product_detail(
    restaurant_slug: str,
    product_slug: str,
    branch_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ProductResponse:
    """Um produto pelo slug, dentro de uma filial.

    **Sem `branch_id`, vale a filial padrao — e e por isso que os links ja
    divulgados continuam funcionando.** A migracao 20260820_0026 deixou as
    linhas que ja existiam na filial padrao, com os mesmos ids e os mesmos
    slugs, entao um link antigo abre exatamente o produto que sempre abriu.

    Produto que existe SO numa filial nao padrao responde 404 pelo link sem
    parametro: sem loja escolhida nao ha preco a mostrar.
    """
    return MenuService(db).get_product_detail(restaurant_slug, product_slug, branch_id)
