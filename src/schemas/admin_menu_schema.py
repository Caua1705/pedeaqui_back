"""Contrato das rotas de cardapio do painel (BLOCO B da Fase 3).

Separado de `product_schema` e `restaurant_schema` de proposito: aqueles sao
o cardapio PUBLICO, que so mostra o que esta ativo e disponivel. Aqui o
lojista precisa ver e escrever tambem o que esta desligado, entao os campos
`is_active` / `is_available` sao parte do contrato em vez de filtro implicito.

Nenhum schema daqui aceita `restaurant_id`: ele sai do token (AdminScope).
"""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from src.schemas.common_schema import BaseResponse


# Teto do nome de categoria/produto/opcao. Bate com o que a coluna aceita
# (Text, sem limite) mas evita que o painel grave um paragrafo no lugar de
# um nome e quebre o layout do cardapio publico.
MAX_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 500
# Teto de itens em uma reordenacao. Uma tela de arrastar-e-soltar manda a
# lista inteira; sem limite, o corpo poderia trazer dez mil ids.
MAX_REORDER_ITEMS = 200


class AdminCategoryResponse(BaseResponse):
    id: UUID
    name: str
    slug: str
    sort_order: int | None = 0
    is_active: bool | None = True


class AdminCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class AdminCategoryUpdate(BaseModel):
    """Edicao parcial: so o que vier no corpo e alterado.

    O slug NAO entra aqui. Ele ja e parte da URL publica do cardapio
    (`/restaurants/{slug}/categories/{category_slug}/products`), e trocar de
    nome nao pode invalidar links que o lojista ja divulgou. Slug e derivado
    do nome uma vez, na criacao.
    """

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    sort_order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class CategoryReorderRequest(BaseModel):
    """Nova ordem das categorias, da primeira para a ultima.

    A lista inteira e nao pares (id, posicao) porque e assim que uma tela de
    arrastar-e-soltar pensa: o painel manda o que esta vendo e o servidor
    numera. Enviar posicoes soltas abriria espaco para duas categorias com o
    mesmo `sort_order` e ordem final imprevisivel.
    """

    category_ids: list[UUID] = Field(min_length=1, max_length=MAX_REORDER_ITEMS)

    @model_validator(mode="after")
    def validate_no_duplicates(self):
        if len(set(self.category_ids)) != len(self.category_ids):
            raise ValueError("category_ids nao pode repetir a mesma categoria")
        return self


class ProductReorderRequest(BaseModel):
    """Nova ordem dos produtos DE UMA CATEGORIA, do primeiro para o ultimo.

    Tem `category_id` e a reordenacao de categorias nao tem equivalente
    porque `sort_order` de produto so significa alguma coisa dentro da
    categoria: o cardapio publico ordena por
    `Category.sort_order, Product.sort_order, Product.name`
    (src/repositories/menu_repository.py:51). Uma lista "completa do
    restaurante" renumeraria produtos de categorias diferentes numa sequencia
    unica, e a ordem dentro de cada categoria passaria a depender de quantos
    produtos vieram antes dela na lista — que nao e nada que o lojista
    arrastou na tela.

    Pelo mesmo motivo, a lista completa exigida e a da CATEGORIA, nao a do
    restaurante: e o conjunto que compartilha a numeracao.
    """

    category_id: UUID
    product_ids: list[UUID] = Field(min_length=1, max_length=MAX_REORDER_ITEMS)

    @model_validator(mode="after")
    def validate_no_duplicates(self):
        if len(set(self.product_ids)) != len(self.product_ids):
            raise ValueError("product_ids nao pode repetir o mesmo produto")
        return self


class AdminOptionResponse(BaseResponse):
    id: UUID
    option_group_id: UUID
    name: str
    description: str | None = None
    additional_price: float
    sort_order: int | None = 0
    is_active: bool


class AdminOptionGroupResponse(BaseResponse):
    id: UUID
    product_id: UUID
    name: str
    description: str | None = None
    min_select: int
    max_select: int
    is_required: bool
    sort_order: int | None = 0
    is_active: bool
    options: list[AdminOptionResponse] = Field(default_factory=list)


class AdminProductResponse(BaseResponse):
    id: UUID
    category_id: UUID
    code: str | None = None
    name: str
    slug: str | None = None
    description: str | None = None
    price: float
    image_path: str | None = None
    image_url: str | None = None
    is_active: bool | None = True
    is_available: bool | None = True
    sort_order: int | None = 0
    # Somente leitura, como `image_path`: quem escreve e
    # PATCH /admin/products/{id}/printing-sector, que confere se o setor e
    # de uma filial deste lojista. Texto livre aqui apontaria o produto para
    # a impressora de outro restaurante.
    printing_sector_id: UUID | None = None


class AdminProductDetailResponse(AdminProductResponse):
    """Produto com os grupos de opcoes, para a tela de edicao.

    A listagem nao traz os grupos: uma tela com 200 produtos faria 200
    subconsultas para mostrar dado que nem aparece na tabela.
    """

    option_groups: list[AdminOptionGroupResponse] = Field(default_factory=list)


class AdminProductListResponse(BaseModel):
    """Pagina de produtos com o total do filtro.

    Mesmo envelope da listagem de pedidos, pelo mesmo motivo: sem `total` o
    painel nao sabe se existe pagina seguinte.
    """

    items: list[AdminProductResponse]
    total: int
    limit: int
    offset: int


class AdminProductCreate(BaseModel):
    category_id: UUID
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    # Decimal e nao float: preco entra na conta do pedido, e float acumula
    # erro de centavo (mesma regra do resto do projeto, ver utils/money).
    price: Decimal = Field(ge=0)
    code: str | None = Field(default=None, max_length=60)
    is_active: bool = True
    is_available: bool = True
    sort_order: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class AdminProductUpdate(BaseModel):
    """Edicao parcial do produto.

    Sem `image_path`: a imagem so muda por POST /admin/products/{id}/image,
    que valida o arquivo e grava no bucket. Se o caminho fosse texto livre
    aqui, o painel poderia apontar o produto para qualquer objeto do
    Storage, inclusive de outro restaurante.

    Sem `slug` pelo mesmo motivo da categoria: ele e URL publica.
    """

    category_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    price: Decimal | None = Field(default=None, ge=0)
    code: str | None = Field(default=None, max_length=60)
    is_active: bool | None = None
    is_available: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ProductAvailabilityRequest(BaseModel):
    """Corpo da acao rapida de esgotado/disponivel (BLOCO B4).

    Rota propria em vez de um PATCH do produto inteiro porque e a operacao
    mais frequente do dia: o atendente marca "acabou a costela" no meio do
    almoco. Corpo de um campo so, sem chance de o painel reenviar preco
    velho junto e desfazer uma edicao feita em outra aba.
    """

    is_available: bool


class ProductImageResponse(BaseModel):
    image_path: str
    image_url: str


class AdminOptionGroupFields(BaseModel):
    """Campos do grupo, com as regras que a selecao precisa respeitar.

    Ficam juntos em uma classe base porque a validacao cruzada (minimo x
    maximo x obrigatorio) tem que valer igual na criacao e na edicao, e a
    edicao valida o resultado da MESCLA com o que ja esta no banco — mesmo
    arranjo de CouponCampaignFields.
    """

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    min_select: int = Field(default=0, ge=0)
    max_select: int = Field(default=1, ge=1)
    is_required: bool = False
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_selection_limits(self):
        if self.max_select < self.min_select:
            raise ValueError("max_select nao pode ser menor que min_select")
        # Um grupo obrigatorio com min_select=0 e uma armadilha: o pedido
        # seria recusado na criacao (order_service valida obrigatoriedade)
        # sem que o cardapio conseguisse explicar o que falta escolher.
        if self.is_required and self.min_select < 1:
            raise ValueError("grupo obrigatorio precisa de min_select maior que zero")
        return self


class AdminOptionGroupCreate(AdminOptionGroupFields):
    pass


class AdminOptionGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    min_select: int | None = Field(default=None, ge=0)
    max_select: int | None = Field(default=None, ge=1)
    is_required: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class AdminOptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    additional_price: Decimal = Field(default=Decimal("0.00"), ge=0)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class AdminOptionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    additional_price: Decimal | None = Field(default=None, ge=0)
    sort_order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None
