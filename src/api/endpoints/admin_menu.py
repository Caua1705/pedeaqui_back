from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    GERENCIA,
    PESSOAS,
    SOMENTE_DONO,
    AdminScope,
    ensure_pode_definir_preco,
    exigir_papel,
    get_admin_scope,
)
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
    ProductReorderRequest,
)
from src.services.admin_menu_service import AdminMenuService


# Mesma regra das outras rotas /admin: restaurante sai do token. Nenhuma
# rota daqui aceita restaurante, categoria ou produto de outro dono — o
# service confere tudo contra `scope.restaurant_id`.
#
# Papeis, neste arquivo:
#
# - LER o cardapio: PESSOAS. O balcao precisa achar o produto na tela para
#   marcar "acabou a picanha", e a busca e a mesma tela da edicao.
# - ESCREVER no cardapio: GERENCIA.
# - ESCREVER PRECO: SOMENTE_DONO, conferido pelo corpo — ver
#   `ensure_pode_definir_preco`.
# - `availability`: PESSOAS, e de proposito. E a acao que a operacao de
#   balcao precisa fazer sozinha as 20h; tirando-a do atendente, a saida
#   pratica vira ele usando a conta do dono, e ai nada disto vale nada.
router = APIRouter(prefix="/admin", tags=["admin menu"])


@router.get(
    "/categories",
    response_model=list[AdminCategoryResponse],
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
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
    dependencies=[Depends(exigir_papel(GERENCIA))],
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
@router.patch(
    "/categories/reorder",
    response_model=list[AdminCategoryResponse],
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
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


@router.patch(
    "/categories/{category_id}",
    response_model=AdminCategoryResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
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


@router.get(
    "/products",
    response_model=AdminProductListResponse,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
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
    # SOMENTE_DONO e nao GERENCIA, ao contrario das outras escritas de
    # cardapio: `price` e OBRIGATORIO em `AdminProductCreate`. Deixar o
    # gerente criar produto e deixa-lo definir preco, que e exatamente o que
    # `ensure_pode_definir_preco` existe para impedir — so que por uma porta
    # onde a checagem de corpo nao teria como recusar sem recusar a rota
    # inteira. Produto novo de gerente sai como pedido ao dono.
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def create_product(
    payload: AdminProductCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminProductResponse:
    return AdminMenuService(db).create_product(scope, payload)


# Declarada ANTES de /products/{product_id} pelo mesmo motivo da de
# categorias: o FastAPI casa na ordem de registro e "reorder" nao e UUID.
@router.patch(
    "/products/reorder",
    response_model=list[AdminProductResponse],
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def reorder_products(
    payload: ProductReorderRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[AdminProductResponse]:
    """Grava a nova ordem dos produtos de uma categoria.

    Uma chamada para a lista inteira, nao uma por item: arrastar um produto
    do fim para o comeco mexe no `sort_order` de todos os que ficaram no
    meio, e mandar isso item a item deixa o cardapio publico numa ordem
    quebrada entre a primeira e a ultima requisicao.

    Espera a lista COMPLETA dos produtos DAQUELA categoria, na ordem
    desejada. Faltando algum, responde 400 — ver
    AdminMenuService.reorder_products.
    """
    return AdminMenuService(db).reorder_products(scope, payload)


@router.get(
    "/products/{product_id}",
    response_model=AdminProductDetailResponse,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def get_product(
    product_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminProductDetailResponse:
    """Produto com os grupos de opcoes, para a tela de edicao."""
    return AdminMenuService(db).get_product(scope, product_id)


@router.patch(
    "/products/{product_id}",
    response_model=AdminProductResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def update_product(
    product_id: UUID,
    payload: AdminProductUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminProductResponse:
    """Edita nome, descricao, categoria, codigo e preco.

    O preco e a unica parte que o gerente nao alcanca — ver
    `ensure_pode_definir_preco`. Ele fica com o resto da tela.
    """
    if payload.price is not None:
        ensure_pode_definir_preco(scope.admin_user)
    return AdminMenuService(db).update_product(scope, product_id, payload)


@router.patch(
    "/products/{product_id}/availability",
    response_model=AdminProductResponse,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
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


@router.post(
    "/products/{product_id}/image",
    response_model=ProductImageResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
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
    dependencies=[Depends(exigir_papel(PESSOAS))],
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
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def create_option_group(
    product_id: UUID,
    payload: AdminOptionGroupCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminOptionGroupResponse:
    return AdminMenuService(db).create_option_group(scope, product_id, payload)


@router.patch(
    "/option-groups/{group_id}",
    response_model=AdminOptionGroupResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
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
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def create_option(
    group_id: UUID,
    payload: AdminOptionCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminOptionResponse:
    """Cria uma opcao dentro do grupo.

    Opcao SEM `additional_price` e a esmagadora maioria delas ("sem cebola",
    "bem passado", "ponto da carne") e continua sendo do gerente. Mandar um
    valor — qualquer valor, inclusive zero — e decisao de preco e exige o
    dono: um adicional de graca custa tanto quanto um desconto.
    """
    # `model_fields_set` e nao `is not None`: `additional_price` tem default
    # 0,00, entao os dois casos chegam aqui com o mesmo valor e so este
    # conjunto sabe qual deles o painel mandou de verdade.
    if "additional_price" in payload.model_fields_set:
        ensure_pode_definir_preco(scope.admin_user)
    return AdminMenuService(db).create_option(scope, group_id, payload)


@router.patch(
    "/options/{option_id}",
    response_model=AdminOptionResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def update_option(
    option_id: UUID,
    payload: AdminOptionUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminOptionResponse:
    if payload.additional_price is not None:
        ensure_pode_definir_preco(scope.admin_user)
    return AdminMenuService(db).update_option(scope, option_id, payload)
