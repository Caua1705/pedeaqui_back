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
    # Mesmo campo do `AdminProductResponse`: o lojista preenche pelo painel e
    # este schema o publica no cardapio. Ele existia nas duas pontas porque o
    # atendente de voz lia daqui — e enquanto so existia do lado do admin,
    # levantava AttributeError em toda busca falada. **A voz saiu em
    # 06/09/2026 e o campo ficou**, sem leitor no backend: e dado do lojista,
    # ja publicado, e tira-lo daqui seria mudanca de contrato (armadilha 16)
    # para apagar informacao que alguem digitou.
    #
    # NULO e "o lojista nao disse", e nao "serve para ninguem" — revisao
    # 20260825_0039.
    serves_people: int | None = None
    price: float
    image_path: str | None = None
    image_url: str | None = None
    is_active: bool | None = True
    is_available: bool | None = True
    sort_order: int | None = 0
    option_groups: list[ProductOptionGroupResponse] = Field(default_factory=list)
