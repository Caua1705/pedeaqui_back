"""A porta do ENTREGADOR: quem ele e, o que esta com ele, saiu, entregou,
quanto fez.

E a QUARTA porta de `OrderStatusChangeService.apply` — depois do PATCH de
status do painel, do cancelamento do painel e do cancelamento pelo cliente.
Mesma regra das outras tres: o writer nao autoriza, a porta autoriza. Aqui
a autorizacao e a ATRIBUICAO: o pedido e do entregador enquanto houver uma
linha aberta em `courier_assignments` com o `courier.id` dele, e nada fora
disso existe para ele (404, nunca 403).

O que a porta acrescenta ao writer e so o que e dela: ate onde o entregador
anda no grafo (`ensure_courier_can_set`, em `order_state_machine.py`), e a
assinatura no historico (`entregador:<nome>`). Cupom, cashback, estorno e o
evento do stream do painel saem do writer, de graca.

**Quem autentica e o PAR** link + codigo, em toda requisicao, sem sessao.
`authenticate` e chamada pela dependencia de rota
(`api/dependencies/courier_auth.py`) e e a unica leitura de credencial.
"""

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.constants import PLATFORM_TIMEZONE
from src.models.courier_model import Courier, CourierAssignment
from src.models.order_model import Order
from src.repositories.branch_repository import BranchRepository
from src.repositories.courier_repository import CourierRepository
from src.schemas.courier_schema import (
    CourierHistoryItem,
    CourierHistoryResponse,
    CourierMeResponse,
    CourierOrderResponse,
    CourierOrdersStatusRequest,
    CourierStatusBatchResponse,
    CourierStatusErrorCode,
    CourierStatusResultItem,
)
from src.services.order_state_machine import (
    COURIER_TRANSITIONS,
    TERMINAL_ORDER_STATUSES,
    ensure_courier_can_set,
)
from src.services.order_status_change_service import OrderStatusChangeService
from src.utils.money import money_to_float
from src.utils.security import (
    hash_courier_link_token,
    utcnow,
    verify_courier_access_code,
    verify_courier_link_token,
)


OUT_FOR_DELIVERY_ROUTE = "POST /courier/{link}/orders/out-for-delivery"
DELIVERED_ROUTE = "POST /courier/{link}/orders/{id}/delivered"

OUT_FOR_DELIVERY = "out_for_delivery"
COMPLETED = "completed"

# Uma resposta so para link desconhecido, regenerado, inativo e excluido: o
# link "morre", e quem o tem nao consegue distinguir por que.
LINK_INVALIDO = "Link inválido"
CODIGO_INVALIDO = "Código de acesso inválido"

# Teto do recorte do historico. Um trimestre cobre "quanto fiz este mes" com
# folga; acima disso e relatorio do dono, nao tela do motoboy.
MAX_HISTORY_DAYS = 92

OPERATION_TIMEZONE = ZoneInfo(PLATFORM_TIMEZONE)


class CourierDeliveryService:
    def __init__(self, db: Session):
        self.db = db
        self.courier_repository = CourierRepository(db)
        self.branch_repository = BranchRepository(db)
        self.status_change_service = OrderStatusChangeService(db)
        # Injetavel, pela convencao da armadilha 51: o historico sem datas e
        # "hoje", e "hoje" precisa poder ser escolhido pelo teste.
        self.clock = utcnow

    # --- Quem e ---------------------------------------------------------------

    def authenticate(self, link_token: str, access_code: str | None) -> Courier:
        """O cadastro que o par abre. 404 para o link, 401 para o codigo.

        Sao codigos diferentes de proposito: o app precisa distinguir "peca
        um link novo ao restaurante" (404) de "digite o codigo de novo"
        (401). O que o 401 revela — que o link existe — ja esta revelado a
        quem tem o link, e o rate limit por IP e o que segura a forca bruta
        sobre os seis digitos.

        As duas conferencias sao `compare_digest` (armadilha 18). A do link
        e a reconferencia depois do WHERE, com a mesma honestidade de
        `get_order_by_tracking_token`: ela compra falha fechada se o WHERE
        deixar de ser igualdade exata um dia.
        """
        courier = self.courier_repository.get_by_link_hash(hash_courier_link_token(link_token))
        if courier is None or not courier.is_active:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=LINK_INVALIDO)
        if not verify_courier_link_token(link_token, courier.access_link_hash):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=LINK_INVALIDO)
        if not access_code:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Código de acesso ausente")
        if not verify_courier_access_code(access_code, link_token, courier.access_code_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=CODIGO_INVALIDO)
        return courier

    def me(self, courier: Courier) -> CourierMeResponse:
        branch = self.branch_repository.get_by_id_and_restaurant(
            courier.branch_id, courier.restaurant_id
        )
        return CourierMeResponse(
            name=courier.name,
            branch_name=branch.name if branch is not None else "",
        )

    # --- O que esta com ele -----------------------------------------------------

    def list_orders(self, courier: Courier) -> list[CourierOrderResponse]:
        """Os pedidos abertos atribuidos a ele, do mais antigo ao mais novo.

        Traz tambem o que ainda esta em preparo: o motoboy que ve o que vem
        a seguir se organiza; `can_leave` diz o que ja pode sair.
        """
        rows = self.courier_repository.list_open_orders_by_courier(
            courier.id, exclude_statuses=TERMINAL_ORDER_STATUSES
        )
        return [self._order_response(assignment, order) for assignment, order in rows]

    # --- Saiu, entregou ---------------------------------------------------------

    def mark_out_for_delivery(
        self, courier: Courier, payload: CourierOrdersStatusRequest
    ) -> CourierStatusBatchResponse:
        """Um ou varios de uma vez, com resposta POR ITEM.

        Cada pedido passa pelo writer sozinho (uma transacao por pedido), e
        e por isso que a resposta nao e tudo-ou-nada: com o terceiro pedido
        em estado errado, os dois primeiros ja sairam e o motoboy ja esta na
        rua — desfazer isso seria mentir para ele.
        """
        items = [self._change_one(courier, order_id, OUT_FOR_DELIVERY) for order_id in payload.order_ids]
        return CourierStatusBatchResponse(items=items)

    def mark_delivered(self, courier: Courier, order_id: uuid.UUID) -> CourierOrderResponse:
        assignment, order = self._get_own_open_order(courier, order_id)
        ensure_courier_can_set(order.status, COMPLETED)
        self._apply(courier, order, COMPLETED, DELIVERED_ROUTE)
        return self._order_response(assignment, order)

    def _change_one(self, courier: Courier, order_id: uuid.UUID, new_status: str) -> CourierStatusResultItem:
        row = self.courier_repository.get_open_order_of_courier(courier.id, order_id)
        if row is None:
            return CourierStatusResultItem(
                order_id=order_id, ok=False, error=CourierStatusErrorCode.NOT_FOUND
            )
        assignment, order = row
        try:
            ensure_courier_can_set(order.status, new_status)
            self._apply(courier, order, new_status, OUT_FOR_DELIVERY_ROUTE)
        except HTTPException as error:
            # O writer e a regra da porta recusam com 409 e uma frase; no lote
            # a frase vira o `message` do item, e o resto do lote segue.
            return CourierStatusResultItem(
                order_id=order_id,
                ok=False,
                error=CourierStatusErrorCode.WRONG_STATUS,
                message=str(error.detail),
            )
        return CourierStatusResultItem(order_id=order_id, ok=True, order=self._order_response(assignment, order))

    def _apply(self, courier: Courier, order: Order, new_status: str, route: str) -> None:
        self.status_change_service.apply(
            order=order,
            # Do PEDIDO: o entregador nao passa restaurante nenhum, e a
            # atribuicao ja o prendeu a um pedido de uma loja so.
            restaurant_id=order.restaurant_id,
            new_status=new_status,
            note=None,
            # Nome e nao id, pelo mesmo motivo do `admin:{email}`: quem le o
            # historico e gente, e "entregador:Zé" diz quem saiu com o pedido.
            changed_by=f"entregador:{courier.name}",
            requester=f"entregador:{courier.id}",
            route=route,
            idempotency_key=None,
        )

    def _get_own_open_order(self, courier: Courier, order_id: uuid.UUID) -> tuple[CourierAssignment, Order]:
        row = self.courier_repository.get_open_order_of_courier(courier.id, order_id)
        # 404 tambem para o pedido que ja terminou: terminal saiu da lista
        # dele, e para a porta ele deixou de existir. 403 ou 409 confirmariam
        # a quem tem o link que aquele UUID e um pedido de verdade.
        if row is None or row[1].status in TERMINAL_ORDER_STATUSES:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado")
        return row

    # --- Quanto fez ------------------------------------------------------------

    def history(
        self,
        courier: Courier,
        start_date: date | None,
        end_date: date | None,
    ) -> CourierHistoryResponse:
        """As entregas concluidas no periodo, e a soma das taxas.

        Sem datas e o dia de hoje, no fuso da operacao: "quanto fiz hoje" e
        a pergunta de toda noite. O fim e exclusivo (o comeco do dia
        seguinte), como nos relatorios do painel.

        Corrida sem taxa (a filial nao tinha taxa configurada na atribuicao)
        conta como ENTREGA e nao como zero, e sai separada em
        `deliveries_without_fee`: e o numero que o motoboy leva ao dono.
        """
        start_at, end_at = self._period_bounds(start_date, end_date)
        rows = self.courier_repository.list_deliveries_by_courier(courier.id, start_at, end_at)

        deliveries = [self._history_item(assignment, order, delivered_at) for assignment, order, delivered_at in rows]
        fees = [assignment.courier_fee_snapshot for assignment, _, _ in rows if assignment.courier_fee_snapshot is not None]
        return CourierHistoryResponse(
            start_date=start_at.astimezone(OPERATION_TIMEZONE).date(),
            end_date=(end_at.astimezone(OPERATION_TIMEZONE) - timedelta(days=1)).date(),
            deliveries_count=len(deliveries),
            deliveries_without_fee=len(deliveries) - len(fees),
            fee_total=money_to_float(sum(fees, Decimal("0"))),
            deliveries=deliveries,
        )

    def _period_bounds(self, start_date: date | None, end_date: date | None) -> tuple[datetime, datetime]:
        today = self.clock().astimezone(OPERATION_TIMEZONE).date()
        start = start_date or today
        end = end_date or start
        if end < start:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="end_date não pode ser anterior a start_date"
            )
        if (end - start).days + 1 > MAX_HISTORY_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Período máximo do histórico: {MAX_HISTORY_DAYS} dias",
            )
        start_at = datetime.combine(start, time.min, tzinfo=OPERATION_TIMEZONE)
        end_at = datetime.combine(end + timedelta(days=1), time.min, tzinfo=OPERATION_TIMEZONE)
        return start_at, end_at

    # --- Respostas ---------------------------------------------------------------

    @staticmethod
    def _order_response(assignment: CourierAssignment, order: Order) -> CourierOrderResponse:
        # O que ha para receber na porta: so no pedido pago na entrega.
        # Pago online, ou pix ainda pendente, o motoboy nao recebe nada — e
        # pedido online nao pago nem chega a `ready`.
        paid_on_delivery = order.payment_status == "on_delivery"
        return CourierOrderResponse(
            order_id=order.id,
            order_number=order.order_number,
            status=order.status,
            can_leave=COURIER_TRANSITIONS.get(order.status) == OUT_FOR_DELIVERY,
            can_deliver=COURIER_TRANSITIONS.get(order.status) == COMPLETED,
            customer_name=order.customer_name_snapshot,
            customer_phone=order.customer_phone_snapshot,
            address_street=order.address_street,
            address_number=order.address_number,
            address_neighborhood=order.address_neighborhood,
            address_complement=order.address_complement,
            address_reference=order.address_reference,
            address_city=order.address_city,
            delivery_latitude=None if order.delivery_latitude is None else float(order.delivery_latitude),
            delivery_longitude=None if order.delivery_longitude is None else float(order.delivery_longitude),
            notes=order.notes,
            payment_method=order.payment_method,
            is_paid=order.payment_status == "paid",
            amount_to_collect=money_to_float(order.total) if paid_on_delivery else 0.0,
            total=money_to_float(order.total),
            courier_fee=(
                None if assignment.courier_fee_snapshot is None else money_to_float(assignment.courier_fee_snapshot)
            ),
            assigned_at=assignment.assigned_at,
            created_at=order.created_at,
        )

    @staticmethod
    def _history_item(assignment: CourierAssignment, order: Order, delivered_at: datetime) -> CourierHistoryItem:
        return CourierHistoryItem(
            order_id=order.id,
            order_number=order.order_number,
            delivered_at=delivered_at,
            address_neighborhood=order.address_neighborhood,
            distance_km=None if assignment.distance_km_snapshot is None else float(assignment.distance_km_snapshot),
            courier_fee=(
                None if assignment.courier_fee_snapshot is None else money_to_float(assignment.courier_fee_snapshot)
            ),
        )
