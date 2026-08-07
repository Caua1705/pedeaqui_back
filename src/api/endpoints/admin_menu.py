from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope, get_admin_scope
from src.api.dependencies.database import get_db
from src.schemas.admin_menu_schema import (
    AdminCategoryCreate,
    AdminCategoryResponse,
    AdminCategoryUpdate,
    AdminOptionCreate,
    AdminOptionGroupCreate,
    AdminOptionGroupResponse,
    AdminOptionGroupUpdate,
    AdminOptionResponse,
    AdminOptionUpdate,
    AdminProductCreate,
    AdminProductDetailResponse,
    AdminProductListResponse,
    AdminProductResponse,
    AdminProductUpdate,
    CategoryReorderRequest,
    ProductAvailabilityRequest,
    ProductImageResponse,
)
from src.services.admin_menu_service import AdminMenuService


# Mesma regra das outras rotas /admin: restaurante sai do token. Nenhuma
# rota daqui aceita restaurante, categoria ou produto de outro dono — o
# service confere tudo contra `scope.restaurant_id`.
router = APIRouter(prefix="/admin", tags=["admin menu"])


@router.get("/categories", response_model=list[AdminCategoryResponse])
def list_categories(
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[AdminCategoryResponse]:
    """Todas as categorias, ativas e inativas.

    Diferente do cardapio publico de proposito: quem desativou uma categoria
    precisa continuar vendo-a para religar.
    """
    return AdminMenuService(db).list_categories(scope)


@router.post(
    "/categories",
    response_model=AdminCategoryResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_category(
    payload: AdminCategoryCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminCategoryResponse:
    return AdminMenuService(db).create_category(scope, payload)


# Declarada ANTES de /categories/{category_id}: o FastAPI casa as rotas na
# ordem de registro, e "reorder" nao e um UUID valido. Invertendo as duas, a
# reordenacao morreria com 422.
@router.patch("/categories/reorder", response_model=list[AdminCategoryResponse])
def reorder_categories(
    payload: CategoryReorderRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[AdminCategoryResponse]:
    """Grava a nova ordem do cardapio.

    Espera a lista COMPLETA das categorias do restaurante, na ordem
    desejada. Faltando alguma, responde 400 — ver
    AdminMenuService.reorder_categories.
    """
    return AdminMenuService(db).reorder_categories(scope, payload)


@router.patch("/categories/{category_id}", response_model=AdminCategoryResponse)
def update_category(
    category_id: UUID,
    payload: AdminCategoryUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminCategoryResponse:
    """Renomeia, reordena ou liga/desliga uma categoria.

    Nao existe DELETE: `order_items` aponta para os produtos dela por FK, e
    apagar quebraria o historico que o cliente ainda consulta. Desativar e
    o que o painel chama de excluir.
    """
    return AdminMenuService(db).update_category(scope, category_id, payload)


@router.get("/products", response_model=AdminProductListResponse)
def list_products(
    category_id: UUID | None = Query(default=None),
    search: str | None = Query(default=None, description="Parte do nome ou do codigo"),
    is_active: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminProductListResponse:
    return AdminMenuService(db).list_products(
        scope,
        category_id=category_id,
        search=search,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/products",
    response_model=AdminProductResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_product(
    payload: AdminProductCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminProductResponse:
    return AdminMenuService(db).create_product(scope, payload)


@router.get("/products/{product_id}", response_model=AdminProductDetailResponse)
def get_product(
    product_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminProductDetailResponse:
    """Produto com os grupos de opcoes, para a tela de edicao."""
    return AdminMenuService(db).get_product(scope, product_id)


@router.patch("/products/{product_id}", response_model=AdminProductResponse)
def update_product(
    product_id: UUID,
    payload: AdminProductUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminProductResponse:
    return AdminMenuService(db).update_product(scope, product_id, payload)


@router.patch("/products/{product_id}/availability", response_model=AdminProductResponse)
def set_product_availability(
    product_id: UUID,
    payload: ProductAvailabilityRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminProductResponse:
    """Marca o produto como esgotado ou disponivel de novo (BLOCO B4).

    Rota separada do PATCH do produto porque e o botao mais usado do dia: um
    corpo de um campo so nao corre o risco de reenviar preco velho junto.
    """
    return AdminMenuService(db).set_product_availability(scope, product_id, payload)


@router.post("/products/{product_id}/image", response_model=ProductImageResponse)
async def upload_product_image(
    product_id: UUID,
    file: UploadFile = File(..., description="JPEG, PNG ou WEBP"),
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> ProductImageResponse:
    """Envia a foto do produto para o bucket do restaurante (BLOCO B5).

    Grava em `<slug-do-restaurante>/products/`, que e a estrutura que o
    bucket ja usa. O tipo e conferido pelos BYTES do arquivo, nao pelo
    content-type declarado — ver src/utils/images.py.

    `async def` porque `UploadFile.read()` e assincrono; a leitura vem do
    arquivo temporario que o Starlette ja montou, nao da rede.
    """
    content = await file.read()
    return AdminMenuService(db).upload_product_image(scope, product_id, content)


@router.get(
    "/products/{product_id}/option-groups",
    response_model=list[AdminOptionGroupResponse],
)
def list_option_groups(
    product_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[AdminOptionGroupResponse]:
    return AdminMenuService(db).list_option_groups(scope, product_id)


@router.post(
    "/products/{product_id}/option-groups",
    response_model=AdminOptionGroupResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_option_group(
    product_id: UUID,
    payload: AdminOptionGroupCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminOptionGroupResponse:
    return AdminMenuService(db).create_option_group(scope, product_id, payload)


@router.patch("/option-groups/{group_id}", response_model=AdminOptionGroupResponse)
def update_option_group(
    group_id: UUID,
    payload: AdminOptionGroupUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminOptionGroupResponse:
    return AdminMenuService(db).update_option_group(scope, group_id, payload)


@router.post(
    "/option-groups/{group_id}/options",
    response_model=AdminOptionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_option(
    group_id: UUID,
    payload: AdminOptionCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminOptionResponse:
    return AdminMenuService(db).create_option(scope, group_id, payload)


@router.patch("/options/{option_id}", response_model=AdminOptionResponse)
def update_option(
    option_id: UUID,
    payload: AdminOptionUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminOptionResponse:
    return AdminMenuService(db).update_option(scope, option_id, payload)
