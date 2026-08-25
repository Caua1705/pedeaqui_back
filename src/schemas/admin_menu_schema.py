"""Contrato das rotas de cardapio do painel (BLOCO B da Fase 3).

Separado de `product_schema` e `restaurant_schema` de proposito: aqueles sao
o cardapio PUBLICO, que so mostra o que esta ativo e disponivel. Aqui o
lojista precisa ver e escrever tambem o que esta desligado, entao os campos
`is_active` / `is_available` sao parte do contrato em vez de filtro implicito.

Nenhum schema daqui aceita `restaurant_id`: ele sai do token (AdminScope).

`branch_id` e outra historia e aparece em UM lugar so, `AdminCategoryCreate`.
Desde a revisao 20260820_0026 categoria e produto pertencem a uma filial, mas
so a categoria precisa dize-lo: o produto herda a filial da CATEGORIA em que
ele nasce. Pedir a filial nos dois abriria a possibilidade de um corpo com os
dois em desacordo, e o unico desfecho possivel para esse corpo seria um 400
que nao precisa existir.
"""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from src.schemas.common_schema import BaseResponse
from src.utils.normalization import normalize_text


# Teto do nome de categoria/produto/opcao. Bate com o que a coluna aceita
# (Text, sem limite) mas evita que o painel grave um paragrafo no lugar de
# um nome e quebre o layout do cardapio publico.
MAX_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 500
# O teto de sanidade de `serves_people`. O CHECK do banco barra zero e negativo
# (revisao 20260825_0039); o exagero fica aqui, onde a recusa vira mensagem de
# tela em vez de erro de banco. Vinte cobre a travessa de churrascaria mais
# generosa que existe e ainda pega o dedo escorregando no teclado.
MAX_SERVES_PEOPLE = 20
# Teto de itens em uma reordenacao. Uma tela de arrastar-e-soltar manda a
# lista inteira; sem limite, o corpo poderia trazer dez mil ids.
MAX_REORDER_ITEMS = 200


class AdminCategoryResponse(BaseResponse):
    id: UUID
    # De qual loja e esta categoria. Obrigatorio na resposta porque a
    # listagem do dono vem com as filiais TODAS: sem o campo, duas categorias
    # "Bebidas" apareceriam como linhas repetidas sem explicacao.
    branch_id: UUID
    name: str
    slug: str
    sort_order: int | None = 0
    is_active: bool | None = True


class AdminCategoryCreate(BaseModel):
    # A filial dona da categoria. OBRIGATORIO e sem default: cair na filial
    # padrao criaria a categoria numa loja que o lojista nao escolheu, e ele
    # so descobriria pelo cardapio publico da outra.
    #
    # Quem esta preso a uma filial e mandar outra recebe 404, pela mesma
    # regra de `AdminScope.ensure_branch_allowed`.
    branch_id: UUID
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_text(value)


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
    def normalize_name(cls, value: str | None) -> str | None:
        return normalize_text(value) if value is not None else None


class CategoryReorderRequest(BaseModel):
    """Nova ordem das categorias DE UMA FILIAL, da primeira para a ultima.

    A lista inteira e nao pares (id, posicao) porque e assim que uma tela de
    arrastar-e-soltar pensa: o painel manda o que esta vendo e o servidor
    numera. Enviar posicoes soltas abriria espaco para duas categorias com o
    mesmo `sort_order` e ordem final imprevisivel.

    `branch_id` entrou na revisao 20260820_0026 e nao e decorativo: o
    conjunto que compartilha a numeracao passou a ser a FILIAL. Sem ele, a
    conferencia de "lista completa" mediria as categorias das lojas todas, e
    o dono com duas lojas nunca conseguiria reordenar uma sem mandar a outra
    junto.
    """

    branch_id: UUID
    category_ids: list[UUID] = Field(min_length=1, max_length=MAX_REORDER_ITEMS)

    @model_validator(mode="after")
    def validate_no_duplicates(self):
        if len(set(self.category_ids)) != len(self.category_ids):
            raise ValueError("category_ids não pode repetir a mesma categoria")
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
            raise ValueError("product_ids não pode repetir o mesmo produto")
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
    # De qual loja sao este preco e esta disponibilidade. Somente leitura: o
    # produto herda a filial da categoria em que nasce, e nao muda de loja
    # depois — ver `AdminProductUpdate`.
    branch_id: UUID
    category_id: UUID
    code: str | None = None
    name: str
    slug: str | None = None
    description: str | None = None
    # Para quantas pessoas serve. NULO e "o lojista nao disse", e nao "serve
    # para ninguem" — ver a revisao 20260825_0039. Campo novo com default nao
    # quebra cliente antigo do contrato (armadilha 7).
    serves_people: int | None = None
    price: float
    image_path: str | None = None
    image_url: str | None = None
    is_active: bool | None = True
    is_available: bool | None = True
    sort_order: int | None = 0
    # A MESMA picanha nas duas lojas, para o relatorio.
    #
    # NAO tem semantica de heranca: preco, nome e disponibilidade continuam
    # sendo de cada linha. Ela so responde "estas duas linhas sao o mesmo
    # item do catalogo", e e o que faz `/admin/reports/products` somar as
    # lojas em vez de listar duas "Picanha".
    #
    # Unica dentro da filial. Repetir a chave de outro produto DA MESMA loja
    # responde 409.
    catalog_key: str | None = None
    # Somente leitura, como `image_path`: quem escreve e
    # PATCH /admin/products/{id}/printing-sector, que confere se o setor e
    # de uma filial deste lojista. Texto livre aqui apontaria o produto para
    # a impressora de outro restaurante.
    printing_sector_id: UUID | None = None
    # POR QUE ESTE PRODUTO SUMIU DO CARDAPIO SEM NINGUEM DESLIGA-LO.
    #
    # `True` quando um grupo obrigatorio ATIVO ficou sem nenhuma opcao ativa.
    # O produto nao tem como ser vendido nesse estado — a cozinha nao produz
    # sem aquela informacao —, entao ele sai do cardapio publico e o pedido de
    # quem ja o tinha no carrinho e recusado.
    #
    # Sem este campo o lojista perde a venda em silencio: `is_active` continua
    # ligado, `is_available` continua ligado, e o produto simplesmente nao
    # aparece para o cliente. E somente leitura — o jeito de resolver e
    # reativar uma opcao do grupo, nao mexer aqui.
    #
    # Default `False` de proposito: campo novo com default nao quebra cliente
    # antigo do contrato (ver armadilha 7).
    unavailable_by_required_group: bool = False


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
    """Produto novo. A FILIAL vem da categoria, e nao do corpo.

    `category_id` ja determina a loja (categoria pertence a uma filial desde
    a revisao 20260820_0026), entao um `branch_id` aqui seria um segundo jeito
    de dizer a mesma coisa — com a chance de os dois discordarem.
    """

    category_id: UUID
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    # Opcional, e o normal e vir vazio. Nao ha default 1: o assistente de voz
    # le nulo como "nao sei" e responde que nao sabe, que e verdade — um
    # default faria o cadastro afirmar um numero que ninguem digitou.
    serves_people: int | None = Field(default=None, ge=1, le=MAX_SERVES_PEOPLE)
    # Decimal e nao float: preco entra na conta do pedido, e float acumula
    # erro de centavo (mesma regra do resto do projeto, ver utils/money).
    price: Decimal = Field(ge=0)
    code: str | None = Field(default=None, max_length=60)
    # Opcional, e o normal e vir vazio: produto sem par em outra loja nao
    # precisa de chave. Quem quiser ligar este a um ja existente copia a
    # `catalog_key` dele — a do `AdminProductResponse` da outra filial.
    catalog_key: str | None = Field(default=None, max_length=120)
    is_active: bool = True
    is_available: bool = True
    sort_order: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_text(value)


class AdminProductUpdate(BaseModel):
    """Edicao parcial do produto.

    Sem `image_path`: a imagem so muda por POST /admin/products/{id}/image,
    que valida o arquivo e grava no bucket. Se o caminho fosse texto livre
    aqui, o painel poderia apontar o produto para qualquer objeto do
    Storage, inclusive de outro restaurante.

    Sem `slug` pelo mesmo motivo da categoria: ele e URL publica.

    Sem `branch_id`: produto nao muda de loja. Mover a linha levaria junto os
    grupos de opcao, o setor de impressao e a chave de catalogo, e deixaria o
    historico de pedido apontando para um produto que a filial nao vende
    mais. Quem quer o item na outra loja cria um la e usa a mesma
    `catalog_key`. Pelo mesmo motivo, `category_id` so aceita categoria DA
    MESMA filial — categoria de outra loja responde 400.
    """

    category_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    serves_people: int | None = Field(default=None, ge=1, le=MAX_SERVES_PEOPLE)
    price: Decimal | None = Field(default=None, ge=0)
    code: str | None = Field(default=None, max_length=60)
    catalog_key: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None
    is_available: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return normalize_text(value) if value is not None else None


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
    def normalize_name(cls, value: str) -> str:
        return normalize_text(value)

    @model_validator(mode="after")
    def validate_selection_limits(self):
        if self.max_select < self.min_select:
            raise ValueError("max_select não pode ser menor que min_select")
        # Um grupo obrigatorio com min_select=0 e uma armadilha: o pedido
        # seria recusado na criacao (order_service valida obrigatoriedade)
        # sem que o cardapio conseguisse explicar o que falta escolher.
        if self.is_required and self.min_select < 1:
            raise ValueError("grupo obrigatório precisa de min_select maior que zero")
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
    def normalize_name(cls, value: str | None) -> str | None:
        return normalize_text(value) if value is not None else None


class AdminOptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    additional_price: Decimal = Field(default=Decimal("0.00"), ge=0)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_text(value)


class AdminOptionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    additional_price: Decimal | None = Field(default=None, ge=0)
    sort_order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return normalize_text(value) if value is not None else None
