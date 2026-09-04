"""Entregadores: o contrato do painel (`/admin/...`) e o do entregador
(`/courier/...`), no mesmo arquivo porque os dois falam do mesmo cadastro.

Dinheiro sai como `float` via `money_to_float`, como o resto do painel
(armadilha 34: nenhuma resposta nova entra na excecao dos dois schemas de
pedido).
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.schemas.admin_report_schema import ReportPeriod
from src.schemas.common_schema import BaseResponse
from src.utils.normalization import normalize_digits, normalize_text


MAX_COURIER_NAME_LENGTH = 120
# O mesmo piso do telefone do cliente (CustomerInput): sem DDD nao ha como
# ligar para o motoboy, que e a unica coisa para que o telefone serve aqui.
MIN_PHONE_DIGITS = 8


def _phone_digits(value: str) -> str:
    phone = normalize_digits(value)
    if len(phone) < MIN_PHONE_DIGITS:
        raise ValueError("telefone invalido")
    return phone


# --- A taxa da filial -------------------------------------------------------


class AdminBranchCourierFeeUpdate(BaseModel):
    """A taxa do entregador DESTA filial. Dois estados por campo:

    - campo ausente do corpo: nao mexe;
    - campo com valor, ou com `null` explicito: grava. `null` e "sem taxa"
      (a atribuicao passa a congelar snapshot nulo), e nao "volta a herdar"
      — aqui nao ha heranca, como nas cinco colunas do frete do cliente.
    """

    courier_fee_base: Decimal | None = Field(default=None, ge=0)
    courier_fee_per_km: Decimal | None = Field(default=None, ge=0)


class AdminBranchCourierFeeResponse(BaseResponse):
    branch_id: UUID
    courier_fee_base: float | None = None
    courier_fee_per_km: float | None = None


# --- O cadastro -------------------------------------------------------------


class AdminCourierCreate(BaseModel):
    """Nome e telefone. Nada mais, de proposito: e o que o dono sabe do
    motoboy, e cada campo a mais e um que ninguem preenche.

    `extra="forbid"` porque o corpo NAO e lugar de escolher codigo de acesso:
    o codigo e sorteado pelo servidor em `POST /admin/couriers/{id}/access`.
    """

    model_config = ConfigDict(extra="forbid")

    branch_id: UUID
    name: str = Field(min_length=1, max_length=MAX_COURIER_NAME_LENGTH)
    phone: str = Field(min_length=MIN_PHONE_DIGITS, max_length=30)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        # NFC, pela armadilha 31: o nome vai para `changed_by` do historico
        # e para a tela do painel, e as duas comparam bytes.
        name = normalize_text(value).strip()
        if not name:
            raise ValueError("nome invalido")
        return name

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return _phone_digits(value)


class AdminCourierUpdate(BaseModel):
    """PATCH parcial. `is_active = false` fecha as corridas abertas e tira o
    acesso na hora; `true` de volta NAO recria o acesso — o par gerado antes
    continua valendo, que e o que "raramente trocado" pede."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=MAX_COURIER_NAME_LENGTH)
    phone: str | None = Field(default=None, min_length=MIN_PHONE_DIGITS, max_length=30)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = normalize_text(value).strip()
        if not name:
            raise ValueError("nome invalido")
        return name

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _phone_digits(value)


class AdminCourierResponse(BaseResponse):
    id: UUID
    branch_id: UUID
    name: str
    phone: str
    is_active: bool
    # Se ja existe um par link+codigo valendo. O par em si nao sai daqui —
    # so em `POST /admin/couriers/{id}/access`, uma vez.
    has_access: bool
    access_generated_at: datetime | None = None
    # Ate quando ele esta travado por errar o codigo. **Nulo e o estado
    # normal**, e o campo so vem preenchido enquanto a trava esta valendo —
    # um instante ja passado faria o painel escrever "travado ate 14h02" as
    # 15h, que e pior que nao dizer nada.
    #
    # E o que o dono precisa ver para atender o telefone: o motoboy travado
    # nao consegue pedir socorro pelo app, e a saida e regenerar o acesso.
    access_blocked_until: datetime | None = None
    created_at: datetime | None = None


class AdminCourierAccessResponse(BaseModel):
    """A UNICA vez que o link e o codigo existem fora do hash.

    Nao ha rota que os devolva de novo, e isso e propriedade: uma rota "me
    mostra de novo" seria uma rota que entrega a credencial de outra pessoa.
    Segunda via e chamar esta rota outra vez, que gera OUTRO par e mata o
    anterior na mesma resposta.

    `link_token` e o segredo do link; o painel monta a URL do app do
    entregador com ele. `access_code` e o que o motoboy digita uma vez.
    """

    courier_id: UUID
    link_token: str
    access_code: str
    access_generated_at: datetime


# --- A atribuicao -----------------------------------------------------------

# Teto do lote. E o que cabe numa tela de "selecionar e atribuir"; acima
# disso e importacao, nao operacao de balcao.
MAX_ORDERS_PER_ASSIGNMENT = 50


class AdminAssignOrdersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_ids: list[UUID] = Field(min_length=1, max_length=MAX_ORDERS_PER_ASSIGNMENT)


class AssignmentErrorCode(str, Enum):
    """Por que um pedido do lote NAO foi atribuido. Enum para a lista sair
    no /openapi.json (armadilha 16): o painel escreve a mensagem por codigo,
    nao pelo texto."""

    # Nao existe, e de outro restaurante, ou de filial que este lojista nao
    # enxerga — os tres sao o mesmo codigo, para nao virar oraculo de UUID.
    NOT_FOUND = "not_found"
    # Retirada nao tem entregador.
    NOT_DELIVERY = "not_delivery"
    # `completed`, `cancelled` ou `rejected`: nao ha mais o que entregar.
    ORDER_CLOSED = "order_closed"
    # O pedido e de outra filial que o lojista enxerga: o motoboy do Centro
    # nao sai com o pedido da Aldeota.
    OTHER_BRANCH = "other_branch"


class AdminAssignmentResponse(BaseResponse):
    id: UUID
    order_id: UUID
    order_number: int
    order_status: str
    courier_id: UUID
    assigned_at: datetime | None = None
    # Nulo = a filial nao tinha taxa configurada quando atribuiu. Nao e zero.
    courier_fee_snapshot: float | None = None
    distance_km_snapshot: float | None = None


class AdminAssignmentResultItem(BaseModel):
    order_id: UUID
    ok: bool
    error: AssignmentErrorCode | None = None
    assignment: AdminAssignmentResponse | None = None


class AdminAssignmentBatchResponse(BaseModel):
    """Um item por `order_id` do corpo, NA MESMA ORDEM.

    Por item e nao tudo-ou-nada: um pedido de retirada no meio do lote nao
    pode derrubar os outros quatro que o atendente selecionou. A escrita
    continua sendo uma so — os itens `ok` sao gravados juntos, e os outros
    nao sao gravados.
    """

    items: list[AdminAssignmentResultItem]


class AdminOrderCourierResponse(BaseModel):
    """Quem esta com o pedido. Os dois nulos = ninguem ainda, que e estado
    normal do pedido e nao um 404."""

    assignment: AdminAssignmentResponse | None = None
    courier: AdminCourierResponse | None = None


# --- O lado do ENTREGADOR ---------------------------------------------------


class CourierMeResponse(BaseModel):
    name: str
    branch_name: str


class CourierOrderResponse(BaseModel):
    """Um pedido nas maos do entregador — o que ele precisa para entregar.

    NAO e o `OrderDetailResponse`: aquele traz itens, valores de desconto e
    o historico inteiro, e o motoboy nao precisa de nada disso. O que ele
    precisa e endereco, telefone, e quanto receber na porta.

    `amount_to_collect` e o total SO quando o pedido e pago na entrega. Pago
    online (ou pix ainda pendente) e zero: nao ha o que receber.
    """

    order_id: UUID
    order_number: int
    status: str
    # O que o botao pode fazer neste estado. Em preparo, nenhum dos dois.
    can_leave: bool
    can_deliver: bool
    customer_name: str
    customer_phone: str
    address_street: str | None = None
    address_number: str | None = None
    address_neighborhood: str | None = None
    address_complement: str | None = None
    address_reference: str | None = None
    address_city: str | None = None
    delivery_latitude: float | None = None
    delivery_longitude: float | None = None
    notes: str | None = None
    payment_method: str | None = None
    is_paid: bool
    amount_to_collect: float
    total: float
    # A taxa DELE nesta corrida, congelada na atribuicao. Nulo = a filial
    # nao tinha taxa configurada.
    courier_fee: float | None = None
    assigned_at: datetime | None = None
    created_at: datetime | None = None
    # O PRAZO PROMETIDO AO CLIENTE, e nao o prazo do motoboy.
    #
    # `delivery_due_at` e o teto da janela: `delivery_estimated_at` (o instante
    # do checkout, quando a promessa foi feita) mais `delivery_eta_max`. E
    # contra ele que a tela conta "+5" (dentro) ou "-5" (passou).
    #
    # A soma e feita AQUI e nao na tela por dois motivos: `delivery_eta_max` e
    # minuto e `delivery_estimated_at` e instante, e somar os dois em cada
    # cliente e a mesma conta escrita em varios lugares (armadilha 54); e
    # `delivery_estimated_at` tem nome de "instante prometido" sem ser — quem
    # o lesse assim mostraria um prazo vencido em todo pedido.
    #
    # NULO quando nao ha promessa (pedido antigo, sem estimativa gravada). A
    # tela nao mostra nada nesse caso — nunca "0 min", que seria "chegou a
    # hora" para um pedido sem prazo nenhum.
    delivery_due_at: datetime | None = None
    # A janela em MINUTOS, como foi prometida: "40 a 55 min". Vem junto do
    # `delivery_due_at` — os tres sao gravados numa escrita so.
    delivery_eta_min: int | None = None
    delivery_eta_max: int | None = None


# Teto do lote de "saiu para entrega". O motoboy leva meia duzia por
# corrida; cinquenta e o mesmo teto da atribuicao pelo painel.
MAX_ORDERS_PER_STATUS_BATCH = 50


class CourierOrdersStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_ids: list[UUID] = Field(min_length=1, max_length=MAX_ORDERS_PER_STATUS_BATCH)


class CourierStatusErrorCode(str, Enum):
    """Por que um pedido do lote NAO mudou. Enum para a lista sair no
    /openapi.json (armadilha 16)."""

    # Nao esta com ele — inclusive o que ja terminou.
    NOT_FOUND = "not_found"
    # Ainda nao esta pronto, ou ja saiu. `message` traz a frase.
    WRONG_STATUS = "wrong_status"


class CourierStatusResultItem(BaseModel):
    order_id: UUID
    ok: bool
    error: CourierStatusErrorCode | None = None
    message: str | None = None
    order: CourierOrderResponse | None = None


class CourierStatusBatchResponse(BaseModel):
    """Um item por `order_id` do corpo, na mesma ordem. Os `ok` JA SAIRAM,
    mesmo que outro do lote tenha falhado."""

    items: list[CourierStatusResultItem]


class CourierHistoryItem(BaseModel):
    order_id: UUID
    order_number: int
    delivered_at: datetime
    address_neighborhood: str | None = None
    distance_km: float | None = None
    courier_fee: float | None = None


class CourierHistoryResponse(BaseModel):
    """"Quanto fiz": as entregas concluidas no periodo e a soma das taxas.

    `deliveries_without_fee` conta as corridas cujo snapshot e nulo (a
    filial nao tinha taxa na atribuicao). Elas entram em
    `deliveries_count` e NAO entram em `fee_total` — e o numero que o
    motoboy leva ao dono para acertar a mao.
    """

    start_date: date
    end_date: date
    deliveries_count: int
    deliveries_without_fee: int
    fee_total: float
    deliveries: list[CourierHistoryItem]


# --- O relatorio do dono ------------------------------------------------------


class AdminCourierFeeReportItem(BaseModel):
    courier_id: UUID
    name: str
    phone: str
    branch_id: UUID
    # Saiu do cadastro, mas fez corridas no periodo e e pago por elas.
    is_deleted: bool
    deliveries_count: int
    # Corridas com taxa NULA (a filial nao tinha taxa na atribuicao). Entram
    # em `deliveries_count` e NAO em `fee_total`: e o numero a acertar a mao.
    deliveries_without_fee: int
    fee_total: Decimal


class AdminCourierFeeReportResponse(BaseModel):
    """Quanto o dono deve a cada motoboy no periodo.

    `Decimal` e nao `float`, como os outros relatorios de dinheiro do painel
    (`CommissionReportResponse`, `SalesSummaryResponse`): e o formato que o
    painel ja le nessa tela, e misturar os dois num mesmo menu e a armadilha
    34 por outra porta.
    """

    restaurant_id: UUID
    branch_id: UUID | None = None
    period: ReportPeriod
    deliveries_count: int
    deliveries_without_fee: int
    fee_total: Decimal
    couriers: list[AdminCourierFeeReportItem]
