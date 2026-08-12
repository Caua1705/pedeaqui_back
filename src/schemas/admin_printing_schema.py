"""Contrato dos setores de impressao e das tarefas de impressao.

Nenhum schema daqui aceita `restaurant_id` nem `branch_id` no corpo: o
restaurante sai do token (AdminScope) e a filial sai do path, onde o service
a confronta com o escopo antes de qualquer leitura ou escrita — mesma regra
de `admin_settings_schema`.
"""

from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from src.schemas.common_schema import BaseResponse
from src.utils.normalization import normalize_text


# Mesmo teto de nome do cardapio. Setor com nome de paragrafo estoura a
# largura da comanda e some da lista do painel.
MAX_NAME_LENGTH = 120

# Os dois tipos de via. `customer` e a do cliente (com dinheiro);
# `production` e a da praca (sem preco algum).
PRINT_JOB_CUSTOMER = "customer"
PRINT_JOB_PRODUCTION = "production"

# Como a tarefa pede a fonte. O agente traduz para o comando da impressora
# dele (ESC/POS, CUPS, o que for) e nada mais: `large` e a unica coisa que o
# backend nao consegue resolver so com texto, porque fonte e um estado da
# impressora, nao um caractere.
FONT_NORMAL = "normal"
FONT_LARGE = "large"

# Nome de impressora do Windows. O limite da API para o nome de uma
# impressora local e 220 caracteres; 255 cobre com folga sem virar campo
# livre de texto.
MAX_PRINTER_NAME_LENGTH = 255

def _clean_printer_name(value: str | None) -> str | None:
    """Tira o espaco das pontas e trata vazio como ausente.

    O nome tem que casar BYTE A BYTE com o do Windows. Um espaco colado
    junto do nome no copiar-e-colar do painel faria a via nao sair — e o
    unico sintoma seria a impressora que nao recebeu nada.

    Nao aplica `normalize_text` (que faz NFC): o nome nao e nosso, e do
    Windows, e ele tem que ser gravado exatamente como aquela maquina o
    reportou.
    """
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


class PrintingSectorResponse(BaseResponse):
    id: UUID
    branch_id: UUID
    name: str
    is_active: bool
    sort_order: int
    # Nulo = o agente resolve pelo config.ini da maquina, como sempre fez.
    printer_name: str | None = None


class PrintingSectorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True
    printer_name: str | None = Field(default=None, max_length=MAX_PRINTER_NAME_LENGTH)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_text(value)

    @field_validator("printer_name")
    @classmethod
    def clean_printer_name(cls, value: str | None) -> str | None:
        return _clean_printer_name(value)


class PrintingSectorUpdate(BaseModel):
    """Edicao parcial: so o que vier no corpo e alterado.

    Desativar um setor e `{"is_active": false}` aqui — nao existe DELETE,
    pelo mesmo motivo do cardapio: `products.printing_sector_id` aponta para
    esta linha por FK, e apagar quebraria o vinculo de todo produto ligado a
    ela. "Excluir" no painel e desativar.

    `branch_id` nao entra: mudar um setor de filial e mudar de impressora
    fisica. Quem quer isso cria o setor na outra filial e reaponta os
    produtos.
    """

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    sort_order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    # `null` explicito aqui e "volte a resolver pelo config.ini", e nao
    # "campo ausente" — o service usa `exclude_unset`, entao os dois casos
    # sao distinguiveis.
    printer_name: str | None = Field(default=None, max_length=MAX_PRINTER_NAME_LENGTH)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return normalize_text(value) if value is not None else None

    @field_validator("printer_name")
    @classmethod
    def clean_printer_name(cls, value: str | None) -> str | None:
        return _clean_printer_name(value)


class ProductPrintingSectorRequest(BaseModel):
    """Vincula (ou desvincula) um produto a um setor.

    `null` nao e ausencia de valor: e a instrucao de NAO imprimir via de
    producao para este produto. E o caso da lata de refrigerante, que sai da
    geladeira do balcao — a comanda dela so gastaria papel e ensinaria a
    cozinha a ignorar comanda.
    """

    printing_sector_id: UUID | None = None


class ProductPrintingSectorResponse(BaseModel):
    """O vinculo depois de gravado.

    Devolve o NOME do setor junto do id para a tela confirmar o que mudou
    sem uma segunda chamada — "Pizza Calabresa -> Forno" e o que o lojista
    precisa ler para saber que acertou.
    """

    product_id: UUID
    printing_sector_id: UUID | None = None
    printing_sector_name: str | None = None


class CategoryPrintingSectorResponse(BaseModel):
    """Resultado da aplicacao em massa por categoria."""

    category_id: UUID
    printing_sector_id: UUID | None = None
    updated_products: int


class PrintJobResponse(BaseModel):
    """Uma bobina a imprimir, ja pronta.

    `content` sai quebrado em `columns` colunas. O agente NAO reformata,
    nao alinha e nao decide fonte: ele seleciona `font_size`, escreve o
    texto e corta. Toda a regra fica no backend (src/services/print_layout.py),
    onde e testavel e onde uma correcao de layout e um deploy, nao uma visita
    a cada loja.
    """

    type: str = Field(description=f"'{PRINT_JOB_CUSTOMER}' ou '{PRINT_JOB_PRODUCTION}'")
    # Nulo na via do cliente, que nao pertence a setor nenhum.
    sector_id: UUID | None = None
    # Preenchido SEMPRE, inclusive na via do cliente ("Via do cliente"): e o
    # que o agente mostra no log e o que sai impresso no topo da bobina.
    sector_name: str
    # Impressora escolhida no painel para este setor. Nulo = o agente
    # resolve pelo config.ini, como antes de a coluna existir — e o que faz
    # toda instalacao antiga continuar imprimindo sem ser reconfigurada.
    #
    # E o campo que conserta o rename silencioso: com ele o agente para de
    # depender do NOME do setor para achar a impressora.
    printer_name: str | None = None
    columns: int
    font_size: str = Field(description=f"'{FONT_NORMAL}' ou '{FONT_LARGE}'")
    content: str


class OrderPrintJobsResponse(BaseModel):
    """As vias de um pedido, na ordem em que devem sair.

    `jobs` pode conter SO a via do cliente: pedido com pagamento online
    ainda nao confirmado nao gera via de producao — a mesma regra do
    "aguardando pagamento, nao preparar" que ja barra o pedido de entrar na
    cozinha (`ensure_payment_allows_order_status`).
    """

    order_id: UUID
    order_number: int
    branch_id: UUID
    jobs: list[PrintJobResponse]

