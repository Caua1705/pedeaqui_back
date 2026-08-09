import logging
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.core.constants import ORDER_STATUSES, PLATFORM_TIMEZONE
from src.models.admin_user_model import AdminUser
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.order_repository import OrderRepository
from src.schemas.admin_order_schema import (
    AdminOrderListItem,
    AdminOrderListResponse,
    AdminOrderStatusCount,
    AdminOrderStatusCountsResponse,
    CancelOrderRequest,
    UpdateOrderStatusRequest,
)
from src.schemas.order_schema import OrderDetailResponse
from src.services.idempotency_service import IdempotencyService
from src.services.order_service import OrderService
from src.services.order_state_machine import (
    ensure_order_transition_allowed,
    ensure_payment_allows_order_status,
)
from src.services.coupon_service import CouponService
from src.utils.money import money_to_float


logger = logging.getLogger("uvicorn.error")

UPDATE_STATUS_ROUTE = "PATCH /admin/orders/{order_id}/status"
CANCEL_ROUTE = "PATCH /admin/orders/{order_id}/cancel"

# O unico destino da rota de cancelamento. Constante para nao aparecer como
# string solta ao lado das outras regras de status.
CANCELLED_STATUS = "cancelled"

PANEL_TIMEZONE = ZoneInfo(PLATFORM_TIMEZONE)

# Teto do recorte da listagem, mesmo motivo do relatorio de comissao: sem
# limite, um start_date=2020-01-01 varre a tabela inteira. Mais folgado que
# o do relatorio (92 dias) porque a listagem e paginada — o que custa aqui e
# o COUNT, nao o volume devolvido.
MAX_LIST_DAYS = 366

# Tamanho maximo do texto de busca. Nao e limite de negocio: e para nao
# montar um ILIKE de dez mil caracteres a partir da querystring.
MAX_SEARCH_LENGTH = 120


class AdminOrderService:
    def __init__(self, db: Session):
        self.db = db
        self.order_repository = OrderRepository(db)
        self.coupon_service = CouponService(db)
        self.idempotency_service = IdempotencyService(db)

    def list_orders(
        self,
        scope: AdminScope,
        branch_id: UUID | None = None,
        order_status: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AdminOrderListResponse:
        """Pagina de pedidos do restaurante do token.

        O restaurante NAO e mais parametro: vinha como slug na URL e era
        confrontado com o token aqui dentro. Confrontar funcionava, mas
        deixava na API uma rota que aceita restaurante de fora — bastava um
        `if` esquecido na proxima rota para virar vazamento. Agora nao ha o
        que esquecer, porque nao ha o que passar.
        """
        self._ensure_valid_status_filter(order_status)
        effective_branch_id = scope.resolve_branch_filter(branch_id)
        start_at, end_at = self._period_bounds(start_date, end_date)
        normalized_search = self._normalize_search(search)

        orders = self.order_repository.list_orders_by_restaurant(
            restaurant_id=scope.restaurant_id,
            branch_id=effective_branch_id,
            status=order_status,
            start_at=start_at,
            end_at=end_at,
            search=normalized_search,
            limit=limit,
            offset=offset,
        )
        total = self.order_repository.count_orders_by_restaurant(
            restaurant_id=scope.restaurant_id,
            branch_id=effective_branch_id,
            status=order_status,
            start_at=start_at,
            end_at=end_at,
            search=normalized_search,
        )
        return AdminOrderListResponse(
            items=[self.to_list_item(order) for order in orders],
            total=total,
            limit=limit,
            offset=offset,
        )

    def count_orders_by_status(
        self,
        scope: AdminScope,
        branch_id: UUID | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        search: str | None = None,
    ) -> AdminOrderStatusCountsResponse:
        """Contadores dos badges, com os mesmos filtros da listagem.

        Aceita os mesmos filtros de proposito: badge que conta o dia inteiro
        em cima de uma lista filtrada por filial nao bate com o que esta na
        tela, e o lojista passa a nao confiar em nenhum dos dois numeros.
        """
        effective_branch_id = scope.resolve_branch_filter(branch_id)
        start_at, end_at = self._period_bounds(start_date, end_date)

        grouped = self.order_repository.count_orders_grouped_by_status(
            restaurant_id=scope.restaurant_id,
            branch_id=effective_branch_id,
            start_at=start_at,
            end_at=end_at,
            search=self._normalize_search(search),
        )
        return AdminOrderStatusCountsResponse(
            counts=[
                AdminOrderStatusCount(status=order_status, count=grouped.get(order_status, 0))
                for order_status in ORDER_STATUSES
            ],
            total=sum(grouped.values()),
        )

    def get_order_detail(self, order_id: UUID, scope: AdminScope) -> OrderDetailResponse:
        order = self._get_order_in_scope(order_id, scope)
        return OrderService.to_order_detail_response(order)

    def update_order_status(
        self,
        order_id: UUID,
        scope: AdminScope,
        payload: UpdateOrderStatusRequest,
        admin_user: AdminUser,
        idempotency_key: str | None = None,
    ) -> OrderDetailResponse:
        if payload.status not in ORDER_STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status inválido")

        return self._apply_status_change(
            order_id=order_id,
            scope=scope,
            new_status=payload.status,
            note=payload.note,
            admin_user=admin_user,
            idempotency_key=idempotency_key,
            route=UPDATE_STATUS_ROUTE,
        )

    def cancel_order(
        self,
        order_id: UUID,
        scope: AdminScope,
        payload: CancelOrderRequest,
        admin_user: AdminUser,
        idempotency_key: str | None = None,
    ) -> OrderDetailResponse:
        """Cancela o pedido exigindo o motivo.

        Rota propria e nao um `status="cancelled"` no PATCH de status por
        causa justamente do motivo: exigi-lo la significaria tornar `note`
        condicionalmente obrigatorio conforme o status, uma regra que nao
        aparece no contrato e que o painel so descobre tomando 422.

        Daqui para baixo o caminho e o MESMO do PATCH de status — mesma
        maquina de estados (cancelar segue proibido a partir de um estado
        final), mesmo estorno de cupom, mesma assinatura no historico. Uma
        segunda escrita de status com regras proprias seria a chance de as
        duas divergirem.
        """
        return self._apply_status_change(
            order_id=order_id,
            scope=scope,
            new_status=CANCELLED_STATUS,
            note=payload.reason,
            admin_user=admin_user,
            idempotency_key=idempotency_key,
            route=CANCEL_ROUTE,
        )

    def _apply_status_change(
        self,
        order_id: UUID,
        scope: AdminScope,
        new_status: str,
        note: str | None,
        admin_user: AdminUser,
        idempotency_key: str | None,
        route: str,
    ) -> OrderDetailResponse:
        """Grava a mudanca de status: validacao, escrita e historico.

        `route` entra no escopo da idempotencia para que a mesma
        `Idempotency-Key` reenviada em rotas diferentes nao seja tratada
        como repeticao de uma so.
        """
        restaurant_id = scope.restaurant_id
        order = self._get_order_in_scope(order_id, scope)

        # Sem isto, cada reenvio do painel (clique duplo, retry) empilhava uma
        # linha nova em order_status_history para o mesmo status, sujando o
        # historico que o cliente ve.
        replayed = self.idempotency_service.begin(
            scope=IdempotencyService.build_scope(
                restaurant_id=restaurant_id,
                route=route,
                requester=f"admin:{admin_user.id}",
            ),
            key=idempotency_key,
            request_fingerprint=IdempotencyService.fingerprint({
                "order_id": str(order_id),
                "status": new_status,
                "note": note,
            }),
        )
        if replayed is not None:
            return IdempotencyService.parse_stored_response(OrderDetailResponse, replayed)

        # A validacao da transicao vem DEPOIS do replay de proposito. Um
        # reenvio da mesma chave chega com o pedido ja no status de destino;
        # validando antes, o retry legitimo morreria com "o pedido ja esta em
        # accepted" em vez de devolver a resposta gravada.
        ensure_order_transition_allowed(order.status, new_status, order.order_type)
        ensure_payment_allows_order_status(new_status, order.payment_status)
        self._log_cancellation_of_paid_order(order, new_status)

        try:
            self.order_repository.update_status(order, new_status)
            if new_status in {"cancelled", "rejected"}:
                self.coupon_service.reverse_for_order(order.id)
            self.order_repository.create_status_history(
                OrderStatusHistory(
                    order_id=order.id,
                    status=new_status,
                    # Quem mudou sai do token, nunca do corpo: o campo era
                    # texto livre enviado pelo cliente, entao o historico
                    # dizia o que o painel quisesse ("sistema", "cliente").
                    changed_by=self._admin_signature(admin_user),
                    note=note,
                )
            )
            if self.idempotency_service.has_reservation:
                # Recarrega antes do commit para gravar a mesma resposta que
                # o chamador vai receber.
                self.idempotency_service.complete(
                    response_body=OrderService.to_order_detail_response(
                        self.order_repository.get_order_detail(order_id, restaurant_id)
                    ).model_dump(mode="json"),
                    order_id=order_id,
                )
            self.db.commit()
            order = self.order_repository.get_order_detail(order_id, restaurant_id)
        except Exception:
            self.db.rollback()
            raise

        return OrderService.to_order_detail_response(order)

    def _get_order_in_scope(self, order_id: UUID, scope: AdminScope):
        """Carrega o pedido conferindo restaurante E filial.

        As duas conferencias ficam juntas porque nao ha caminho para uma sem
        a outra: o filtro por restaurante mora no WHERE do repositorio, e o
        de filial so pode ser feito depois de ler `order.branch_id`.

        Mesmo 404 para "nao existe", "e de outro restaurante" e "e de outra
        filial": distinguir os tres transformaria a rota em um oraculo de
        quais UUIDs de pedido existem na plataforma.
        """
        order = self.order_repository.get_order_detail(order_id, scope.restaurant_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado")
        if not scope.sees_all_branches and order.branch_id != scope.branch_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado")
        return order

    @staticmethod
    def to_list_item(order) -> AdminOrderListItem:
        """Pedido no formato da lista do painel.

        Publico e estatico porque o stream SSE emite exatamente este objeto
        dentro do evento — se o formato divergisse, o pedido que chega pelo
        stream nao encaixaria na mesma linha da tabela que o painel ja
        desenha.
        """
        return AdminOrderListItem(
            id=order.id,
            order_number=order.order_number,
            branch_id=order.branch_id,
            customer_name_snapshot=order.customer_name_snapshot,
            customer_phone_snapshot=order.customer_phone_snapshot,
            order_type=order.order_type,
            status=order.status,
            payment_method=order.payment_method,
            payment_status=order.payment_status,
            total=money_to_float(order.total),
            created_at=order.created_at,
        )

    @staticmethod
    def _ensure_valid_status_filter(order_status: str | None) -> None:
        if order_status and order_status not in ORDER_STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status inválido")

    @staticmethod
    def _normalize_search(search: str | None) -> str | None:
        """Texto de busca pronto para o repositorio, ou None.

        String em branco vira None para nao virar um ILIKE '%%' que casa com
        tudo e ainda paga o custo do scan.
        """
        if search is None:
            return None
        cleaned = search.strip()
        if not cleaned:
            return None
        return cleaned[:MAX_SEARCH_LENGTH]

    @staticmethod
    def _period_bounds(
        start_date: date | None,
        end_date: date | None,
    ) -> tuple[datetime | None, datetime | None]:
        """Recorte de datas do painel convertido para instantes.

        As datas chegam no fuso da operacao (America/Fortaleza), nao em UTC:
        "os pedidos de hoje" para o lojista sao os do dia dele. Sem essa
        conversao, tres horas de pedidos cairiam no dia errado.

        O fim e o comeco do dia SEGUINTE para nao perder o pedido gravado as
        23:59:59.7 — o repositorio usa `<` nesse limite. Mesma regra do
        relatorio de comissao (admin_report_service._period_bounds), so que
        aqui os dois lados sao opcionais e podem vir sozinhos.
        """
        if start_date and end_date and end_date < start_date:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="end_date nao pode ser anterior a start_date",
            )
        if start_date and end_date and (end_date - start_date).days + 1 > MAX_LIST_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Periodo maximo da listagem: {MAX_LIST_DAYS} dias",
            )

        start_at = (
            datetime.combine(start_date, time.min, tzinfo=PANEL_TIMEZONE)
            if start_date
            else None
        )
        end_at = (
            datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=PANEL_TIMEZONE)
            if end_date
            else None
        )
        return start_at, end_at

    @staticmethod
    def _admin_signature(admin_user: AdminUser) -> str:
        """Identidade gravada em order_status_history.changed_by.

        E-mail e nao id porque quem le esse historico e gente (suporte,
        lojista), e um UUID nao diz nada sem outra consulta.
        """
        return f"admin:{admin_user.email}"

    @staticmethod
    def _log_cancellation_of_paid_order(order, new_status: str) -> None:
        """Avisa quando um pedido JA PAGO e cancelado.

        Cancelar nao estorna: o estorno so acontece quando o gateway avisa
        (PaymentService.handle_webhook) ou quando alguem o faz no painel do
        proprio gateway. Enquanto o Mercado Pago nao estiver plugado, este
        log e o unico rastro de que existe dinheiro do cliente parado.
        """
        if new_status in {"cancelled", "rejected"} and order.payment_status == "paid":
            logger.warning(
                "[Pagamento] pedido pago foi %s sem estorno automatico order_id=%s",
                new_status,
                order.id,
            )
