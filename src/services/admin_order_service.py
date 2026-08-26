import logging
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.core.constants import ORDER_STATUSES, PLATFORM_TIMEZONE
from src.models.admin_user_model import AdminUser
from src.repositories.order_repository import OrderRepository
from src.schemas.admin_order_schema import (
    AdminOrderListItem,
    AdminOrderListResponse,
    AdminOrderStatusCount,
    AdminOrderStatusCountsResponse,
    CancelOrderErrorCode,
    CancelOrderErrorDetail,
    CancelOrderRequest,
    UpdateOrderStatusRequest,
)
from src.schemas.order_schema import OrderDetailResponse
from src.services.order_service import OrderService
from src.services.order_state_machine import cancellation_needs_confirmation
from src.services.order_status_change_service import OrderStatusChangeService
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
        # A ESCRITA de status nao mora mais aqui: ela e compartilhada com o
        # cancelamento pelo cliente, e uma copia por porta seria a chance de
        # o cupom voltar num caminho e nao no outro. Ver
        # OrderStatusChangeService.
        self.status_change_service = OrderStatusChangeService(db)

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
            confirm_prepared_order=payload.confirm_prepared_order,
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
            confirm_prepared_order=payload.confirm_prepared_order,
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
        confirm_prepared_order: bool = False,
    ) -> OrderDetailResponse:
        """Carrega o pedido do escopo, confere o que e regra DESTA porta e
        delega a escrita.

        A escrita em si mora em `OrderStatusChangeService`, compartilhada com
        o cancelamento pelo cliente. O que sobra aqui e o que so vale para o
        painel: o escopo do lojista e a confirmacao de pedido em preparo.
        """
        order = self._get_order_in_scope(order_id, scope)
        self._ensure_cancellation_confirmed(order, new_status, confirm_prepared_order)

        return self.status_change_service.apply(
            order=order,
            restaurant_id=scope.restaurant_id,
            new_status=new_status,
            note=note,
            # Quem mudou sai do token, nunca do corpo: o campo era texto
            # livre enviado pelo cliente, entao o historico do pedido dizia
            # o que o painel quisesse ("sistema", "cliente").
            changed_by=self._admin_signature(admin_user),
            requester=f"admin:{admin_user.id}",
            route=route,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _ensure_cancellation_confirmed(
        order,
        new_status: str,
        confirm_prepared_order: bool,
    ) -> None:
        """Exige o segundo clique para cancelar comida que ja foi feita.

        **428 e nao 409, e a escolha e o contrato.** Os 409 desta rota sao
        conflitos de estado de verdade ("pedido ja entregue nao muda mais") e
        saem com `detail` de texto; este aqui nao e erro nenhum — e o backend
        pedindo uma precondicao que o painel consegue satisfazer na hora, com
        um corpo TIPADO que diz qual dialogo abrir. Sobrepor os dois no mesmo
        codigo obrigaria o painel a distinguir pelo texto da mensagem, e
        publicar um `model` de 409 que so vale para metade dos 409 da rota
        seria pior ainda (armadilha 16).

        A checagem roda ANTES do replay de idempotencia, e isso e inofensivo:
        um retry legitimo chega com o pedido ja em `cancelled`, que nao esta
        em PREPARED_ORDER_STATUSES — a confirmacao nao e exigida de novo e a
        resposta gravada volta normalmente.

        Vale para as DUAS rotas do painel. `PATCH /status` aceita
        `status='cancelled'`, e a confirmacao so na rota de cancelamento
        deixaria de pe exatamente a porta que ela existe para fechar.
        """
        if confirm_prepared_order:
            return
        if not cancellation_needs_confirmation(order.status, new_status):
            return
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            # mode="json" para o `code` sair como a string do enum: o dict vai
            # direto para o corpo da resposta.
            detail=CancelOrderErrorDetail(
                code=CancelOrderErrorCode.CONFIRMATION_REQUIRED,
                message=(
                    "Este pedido já está em produção. Cancelar agora não "
                    "devolve o custo da comida para o restaurante. Confirme "
                    "para continuar."
                ),
                order_status=order.status,
            ).model_dump(mode="json"),
        )

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
                detail="end_date não pode ser anterior a start_date",
            )
        if start_date and end_date and (end_date - start_date).days + 1 > MAX_LIST_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Período máximo da listagem: {MAX_LIST_DAYS} dias",
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
