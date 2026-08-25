from uuid import UUID

from pydantic import Field

from src.schemas.common_schema import BaseResponse


class ProductOptionResponse(BaseResponse):
    id: UUID
    name: str
    description: str | None = None
    additional_price: float
    sort_order: int | None = 0


class ProductOptionGroupResponse(BaseResponse):
    id: UUID
    name: str
    description: str | None = None
    min_select: int
    max_select: int
    is_required: bool
    sort_order: int | None = 0
    options: list[ProductOptionResponse]


class ProductResponse(BaseResponse):
    """Um produto do cardapio de UMA filial.

    `restaurant_id` e `branch_id` aparecem os dois: o primeiro por ja ser
    contrato publicado, o segundo porque desde a revisao 20260820_0026 e ele
    que diz de qual LOJA sao este preco e esta disponibilidade. Dois produtos
    com o mesmo nome e precos diferentes so se distinguem por ele.
    """

    id: UUID
    restaurant_id: UUID
    branch_id: UUID
    category_id: UUID
    code: str | None = None
    name: str
    slug: str | None = None
    description: str | None = None
    # Mesmo campo do `AdminProductResponse`, e ele precisa existir NAS DUAS
    # pontas: o lojista preenche pelo painel, e quem le e o atendente de voz,
    # que monta a linha do produto a partir DESTE schema (a hidratacao do
    # `/chat` e da voz passa por `MenuService.product_response`). Enquanto ele
    # so existia do lado do admin, `_serve_quantas_pessoas` levantava
    # AttributeError em toda busca de voz.
    #
    # NULO e "o lojista nao disse", e nao "serve para ninguem" — revisao
    # 20260825_0039. Campo novo com default nao quebra cliente antigo do
    # contrato (armadilha 7), e este schema nao e gravado em
    # `idempotency_keys.response_body`.
    serves_people: int | None = None
    price: float
    image_path: str | None = None
    image_url: str | None = None
    is_active: bool | None = True
    is_available: bool | None = True
    sort_order: int | None = 0
    option_groups: list[ProductOptionGroupResponse] = Field(default_factory=list)
