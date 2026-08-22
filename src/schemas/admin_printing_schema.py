"""Contrato dos setores de impressao e das tarefas de impressao.

Nenhum schema daqui aceita `restaurant_id` nem `branch_id` no corpo: o
restaurante sai do token (AdminScope) e a filial sai do path, onde o service
a confronta com o escopo antes de qualquer leitura ou escrita — mesma regra
de `admin_settings_schema`.
"""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from src.schemas.common_schema import BaseResponse
from src.utils.normalization import normalize_receipt_text, normalize_text


# Mesmo teto de nome do cardapio. Setor com nome de paragrafo estoura a
# largura da comanda e some da lista do painel.
MAX_NAME_LENGTH = 120

# Teto da mensagem do rodape, em caracteres: cinco linhas cheias da bobina de
# 80mm (5 x RECEIPT_WIDTH). Nao e limite de banco — a coluna e `text` — e sim
# de BOBINA: o rodape e espaco de graca no papel que ja sai, e deixa de ser
# de graca quando vira meio metro de propaganda em todo pedido.
MAX_FOOTER_MESSAGE_LENGTH = 240

# Teto de linhas, contado DEPOIS da limpeza. O limite de caracteres sozinho
# nao segura: "a\nb\nc\n..." cabe em 240 caracteres e sai com 120 linhas.
MAX_FOOTER_MESSAGE_LINES = 6

# Teto de copias de uma via. Espelha o CHECK `ck_branches_print_*` da revisao
# 20260821_0029: aqui vira 422 com mensagem, la vira a garantia de que nada
# escreve por fora. Cinco e generoso para o caso real (duas na sacola, uma
# para o motoboy) e barato para o dedo que erra a tecla.
MAX_PRINT_COPIES = 5

# As quatro contagens, juntas onde a regra que vale para as quatro precisa
# percorre-las. Nao herdam nada e sao NOT NULL — a lista existe para que
# acrescentar uma quinta nao dependa de alguem lembrar de todos os lugares.
COPY_FIELDS = (
    "print_customer_copies_delivery",
    "print_production_copies_delivery",
    "print_customer_copies_pickup",
    "print_production_copies_pickup",
)

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

# Os comandos que o painel manda para o agente. Um so, por enquanto. Vive
# como `str, Enum` e nao como constante solta porque assim a LISTA sai no
# /openapi.json e o painel consegue conhecer os valores sem ler o backend
# (armadilha 16).


class PrintAgentCommandType(str, Enum):
    PRINT_TEST = "print_test"


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


def clean_footer_message(value: str | None) -> str | None:
    """A mensagem do rodape pronta para gravar, ou 422 com o motivo.

    `None` atravessa como `None` porque nos dois schemas que usam esta
    funcao ele tem significado proprio: na filial e "volta a herdar", no
    restaurante e "nao ha mensagem". Confundir os dois com string vazia
    tiraria da filial o unico jeito de recusar o texto da marca.

    A limpeza em si e de `normalize_receipt_text`, que e quem sabe o que uma
    termica aceita (o caractere de controle e o motivo dela existir). Aqui
    fica so o teto de LINHAS, e ele mora na escrita de proposito: cortar na
    hora de imprimir faria o lojista descobrir o limite pela bobina, com o
    texto ja gravado e a tela mostrando o que ele digitou.
    """
    if value is None:
        return None

    cleaned = normalize_receipt_text(value)
    if cleaned.count("\n") + 1 > MAX_FOOTER_MESSAGE_LINES:
        raise ValueError(
            f"a mensagem do rodape pode ter no maximo {MAX_FOOTER_MESSAGE_LINES} linhas"
        )
    return cleaned


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


class BranchPrintSettingsResponse(BaseResponse):
    """Como a comanda desta filial e impressa (revisao 20260821_0029).

    Duas coisas numa tela so porque sao a mesma pergunta do lojista — "como
    a minha comanda sai?" —, e porque as duas so existem por filial.

    **Os dois campos de mensagem nao sao redundantes**, pelo mesmo motivo do
    par `overrides`/`effective` da tela de operacao: `receipt_footer_message`
    e o que ESTA FILIAL gravou e alimenta o campo de edicao (nulo = herdando,
    e o campo aparece vazio); `effective_receipt_footer_message` e o que vai
    sair na bobina, ja resolvido com o padrao do restaurante. Publicar so o
    efetivo faria toda filial parecer divergente; so a sobrescrita, faria a
    tela nao ter como mostrar o que o cliente vai ler.

    As quatro contagens nao tem par: elas nao herdam nada.
    """

    branch_id: UUID
    # Nulo = herda de `restaurant_settings`. Vazio = esta filial escolheu
    # nao imprimir rodape. Sao estados diferentes; ver `BranchPrintSettingsUpdate`.
    receipt_footer_message: str | None = None
    effective_receipt_footer_message: str | None = None
    print_customer_copies_delivery: int
    print_production_copies_delivery: int
    print_customer_copies_pickup: int
    print_production_copies_pickup: int


class BranchPrintSettingsUpdate(BaseModel):
    """Edicao parcial: so o que vier no corpo e alterado.

    `receipt_footer_message` tem TRES estados, e os tres sao usados:

    - **ausente do corpo** — nao mexe;
    - **`null`** — esta filial volta a HERDAR a mensagem do restaurante;
    - **`""`** — esta filial NAO imprime rodape, nem o dela nem o da marca.

    Sem o terceiro, a loja que nao quer a campanha da rede nao teria como
    recusa-la: qualquer valor que ela gravasse sairia impresso. E por isso o
    service usa `exclude_unset` e nao `exclude_none` — o segundo apagaria a
    diferenca entre o primeiro e o segundo estado.

    **Zero copias e valido, e e o pedido que originou a feature**: a
    retirada normalmente nao precisa da via do cliente (a que vai grampeada
    na sacola). Zero na via de PRODUCAO tambem passa — e a loja de um
    quiosque so, onde quem monta o pedido e quem o vende — mas vale saber
    que ela desliga a comanda da cozinha daquele tipo de pedido.

    Zerar as DUAS do mesmo tipo e permitido e nao e recusado aqui, mas tem
    um efeito de segunda ordem que vale conhecer: a lista de vias sai vazia,
    e o agente instalado em campo trata lista vazia como "pagamento ainda
    nao confirmado?" no log dele — mensagem errada para este caso, que nao
    da para corrigir sem visitar a loja. Nada quebra (o pedido so nao e
    marcado como impresso, e o replay para em uma hora); a linha do log e
    que mente.
    """

    receipt_footer_message: str | None = Field(
        default=None, max_length=MAX_FOOTER_MESSAGE_LENGTH
    )
    print_customer_copies_delivery: int | None = Field(
        default=None, ge=0, le=MAX_PRINT_COPIES
    )
    print_production_copies_delivery: int | None = Field(
        default=None, ge=0, le=MAX_PRINT_COPIES
    )
    print_customer_copies_pickup: int | None = Field(
        default=None, ge=0, le=MAX_PRINT_COPIES
    )
    print_production_copies_pickup: int | None = Field(
        default=None, ge=0, le=MAX_PRINT_COPIES
    )

    @field_validator("receipt_footer_message")
    @classmethod
    def clean_message(cls, value: str | None) -> str | None:
        return clean_footer_message(value)

    @model_validator(mode="after")
    def validate_copies_are_not_null(self):
        """`null` numa contagem e 422, e nao 500.

        As quatro colunas sao `NOT NULL` e nao herdam nada: `null` nelas nao
        significa coisa nenhuma. Sem esta conferencia o valor atravessaria o
        `exclude_unset` ate o `setattr`, e o `IntegrityError` do Postgres
        viraria 500 numa tela de configuracao — com uma mensagem sobre
        restricao de coluna, que nao diz ao lojista o que fazer.

        O tipo continua `int | None` porque e assim que "campo ausente" se
        declara num corpo parcial. O que se recusa aqui e o `null`
        ESCRITO — `model_fields_set` e o que separa os dois, o mesmo
        mecanismo que o `exclude_unset` do service usa do outro lado.
        """
        for field in COPY_FIELDS:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(
                    f"{field} nao aceita null: a contagem de vias nao herda "
                    "nada do restaurante. Para nao imprimir esta via, mande 0."
                )
        return self


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

    **Copia e ENTRADA REPETIDA nesta lista, e nao um campo `copies`.** A
    filial que pediu duas vias do cliente recebe dois itens identicos, um
    atras do outro. Um campo novo seria mais bonito e o agente nao saberia
    le-lo: **nao existe atualizacao remota do agente** — ele e um `.exe`
    instalado a mao, e cada versao nova e uma visita por loja. Repetindo a
    entrada, a feature vale hoje em toda instalacao ja em campo, inclusive
    nas anteriores a esta revisao.

    `jobs` tambem pode vir VAZIA: a filial que zerou as duas contagens
    daquele tipo de pedido nao imprime nada. O agente ja trata a lista vazia
    (ele avisa no log e nao marca o pedido como impresso).
    """

    order_id: UUID
    order_number: int
    branch_id: UUID
    jobs: list[PrintJobResponse]


# ---------------------------------------------------------------------------
# O agente: sinal de vida, impressoras da maquina e comandos do painel
# ---------------------------------------------------------------------------


class PrintAgentHeartbeatRequest(BaseModel):
    """O que o agente conta sobre si a cada sinal.

    Nao tem `branch_id`: a filial sai do token, como em toda rota /admin. Um
    agente que pudesse escolher a filial no corpo poderia se anunciar como
    outra loja.
    """

    agent_version: str | None = Field(default=None, max_length=40)


class PrintAgentStatusResponse(BaseResponse):
    """O que o painel mostra no bloco "Agente" da tela de Impressao."""

    branch_id: UUID
    agent_version: str | None = None
    last_seen_at: datetime | None = None
    seconds_since_last_seen: int | None = None
    # Calculado, nao gravado: "online" e uma pergunta sobre o AGORA, e uma
    # coluna booleana no banco ficaria mentindo assim que o agente caisse
    # sem avisar.
    is_online: bool


class PrintAgentPrinterInput(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_PRINTER_NAME_LENGTH)
    is_default: bool = False

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = _clean_printer_name(value)
        if cleaned is None:
            raise ValueError("nome de impressora vazio")
        return cleaned


class PrintAgentPrintersRequest(BaseModel):
    """A lista COMPLETA de impressoras daquela maquina.

    Substitui a anterior inteira, e nao acrescenta: impressora removida do
    Windows tem que sumir do seletor do painel, senao o lojista escolhe uma
    que nao existe mais e a via nao sai.
    """

    printers: list[PrintAgentPrinterInput] = Field(default_factory=list, max_length=50)


class PrintAgentPrinterResponse(BaseResponse):
    name: str
    is_default: bool
    reported_at: datetime


class PrintAgentPrintersResponse(BaseModel):
    branch_id: UUID
    printers: list[PrintAgentPrinterResponse]


class PrintTestRequest(BaseModel):
    """Para onde mandar a via de teste.

    Os dois campos sao opcionais e a ordem de resolucao e:
    `printer_name` > a impressora do setor > a padrao do agente. Mandar o
    setor e o caso comum ("testar a Cozinha"); mandar a impressora direto e
    o que serve para conferir uma maquina recem-instalada, antes de existir
    setor nenhum.
    """

    printing_sector_id: UUID | None = None
    printer_name: str | None = Field(default=None, max_length=MAX_PRINTER_NAME_LENGTH)

    @field_validator("printer_name")
    @classmethod
    def clean_printer_name(cls, value: str | None) -> str | None:
        return _clean_printer_name(value)


class PrintTestResponse(BaseModel):
    """O comando foi enfileirado — nao "a via saiu".

    A diferenca importa para a tela: o agente pode estar offline, e quem
    responde se a bobina saiu e a pessoa que esta olhando a impressora. O
    painel usa `agent_is_online` para avisar antes de o lojista ficar
    esperando.
    """

    command_id: UUID
    branch_id: UUID
    created_at: datetime
    agent_is_online: bool
