"""O lado do PAINEL da frente de entregadores.

Tres assuntos, e os tres ficam no mesmo service porque partilham o mesmo
escopo (a filial do token) e o mesmo `_get_branch`: a taxa que a filial
paga, o cadastro dos motoboys, e a atribuicao de pedido a motoboy.

O que este arquivo NAO faz: escrever status de pedido. Quem escreve e
`OrderStatusChangeService`, e a porta do entregador e
`CourierDeliveryService`.
"""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.models.branch_model import Branch
from src.models.courier_model import Courier, CourierAssignment
from src.models.order_model import Order
from src.repositories.branch_repository import BranchRepository
from src.repositories.courier_repository import CourierRepository
from src.repositories.order_repository import OrderRepository
from src.schemas.courier_schema import (
    AdminAssignmentBatchResponse,
    AdminAssignmentResponse,
    AdminAssignmentResultItem,
    AdminAssignOrdersRequest,
    AdminBranchCourierFeeResponse,
    AdminBranchCourierFeeUpdate,
    AdminCourierAccessResponse,
    AdminCourierCreate,
    AdminCourierFeeReportItem,
    AdminCourierFeeReportResponse,
    AdminCourierResponse,
    AdminCourierUpdate,
    AdminOrderCourierResponse,
    AssignmentErrorCode,
)
from src.schemas.admin_report_schema import ReportPeriod
from src.services.admin_report_service import MAX_REPORT_DAYS
from src.services.courier_fee import calculate_courier_fee
from src.utils.date_window import period_bounds
from src.services.order_state_machine import TERMINAL_ORDER_STATUSES
from src.utils.money import money_to_float, quantize_money, to_decimal
from src.utils.security import (
    generate_courier_access_code,
    generate_courier_link_token,
    hash_courier_access_code,
    hash_courier_link_token,
    utcnow,
)


TELEFONE_EM_USO = "Já existe um entregador com este telefone nesta filial"
ENTREGADOR_NAO_ENCONTRADO = "Entregador não encontrado"


class AdminCourierService:
    def __init__(self, db: Session):
        self.db = db
        self.branch_repository = BranchRepository(db)
        self.courier_repository = CourierRepository(db)
        self.order_repository = OrderRepository(db)

    # --- A taxa da filial ---------------------------------------------------

    def get_courier_fee(
        self, scope: AdminScope, branch_id: uuid.UUID
    ) -> AdminBranchCourierFeeResponse:
        branch = self._get_branch(scope, branch_id)
        return self._courier_fee_response(branch)

    def update_courier_fee(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: AdminBranchCourierFeeUpdate,
    ) -> AdminBranchCourierFeeResponse:
        """Grava a taxa desta filial. Campo ausente nao e tocado; `null`
        explicito apaga — e `exclude_unset` que separa os dois."""
        branch = self._get_branch(scope, branch_id)
        changes = payload.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(branch, field, None if value is None else quantize_money(value))
        self._commit()
        return self._courier_fee_response(branch)

    # --- O cadastro ---------------------------------------------------------

    def list_couriers(
        self, scope: AdminScope, branch_id: uuid.UUID | None = None
    ) -> list[AdminCourierResponse]:
        effective_branch_id = scope.resolve_branch_filter(branch_id)
        couriers = self.courier_repository.list_by_restaurant(
            scope.restaurant_id, branch_id=effective_branch_id
        )
        return [self._courier_response(courier) for courier in couriers]

    def create_courier(self, scope: AdminScope, payload: AdminCourierCreate) -> AdminCourierResponse:
        branch = self._get_branch(scope, payload.branch_id)
        self._ensure_phone_is_free(branch.id, payload.phone)
        courier = Courier(
            restaurant_id=scope.restaurant_id,
            branch_id=branch.id,
            name=payload.name,
            phone=payload.phone,
            is_active=True,
        )
        self.courier_repository.create(courier)
        self._commit()
        return self._courier_response(courier)

    def get_courier(self, scope: AdminScope, courier_id: uuid.UUID) -> AdminCourierResponse:
        return self._courier_response(self._get_courier(scope, courier_id))

    def update_courier(
        self,
        scope: AdminScope,
        courier_id: uuid.UUID,
        payload: AdminCourierUpdate,
    ) -> AdminCourierResponse:
        courier = self._get_courier(scope, courier_id)
        changes = payload.model_dump(exclude_unset=True)

        if changes.get("phone") is not None:
            self._ensure_phone_is_free(courier.branch_id, changes["phone"], exclude_courier_id=courier.id)
        for field, value in changes.items():
            if value is not None:
                setattr(courier, field, value)

        # Desativar devolve os pedidos abertos para a fila: inativo nao
        # consegue mais marcar nada (a dependencia recusa o link), e um
        # pedido que ficasse com ele so apareceria ao painel como
        # "atribuido". Reativar NAO reabre: o painel reatribui.
        if changes.get("is_active") is False:
            self.courier_repository.mark_open_assignments_unassigned(
                courier.id, scope.admin_user.id, except_order_statuses=TERMINAL_ORDER_STATUSES
            )
        self._commit()
        return self._courier_response(courier)

    def delete_courier(self, scope: AdminScope, courier_id: uuid.UUID) -> None:
        """Exclusao logica: `deleted_at`, acesso revogado, corridas fechadas.

        DELETE de verdade nao existe porque `courier_assignments` referencia
        este cadastro e o historico e o que o dono usa para pagar — ele tem
        que sobreviver ao motoboy que saiu. Excluido some de toda lista e de
        toda leitura; o telefone fica livre para um cadastro novo.
        """
        courier = self._get_courier(scope, courier_id)
        courier.deleted_at = utcnow()
        courier.is_active = False
        courier.access_link_hash = None
        courier.access_code_hash = None
        self.courier_repository.mark_open_assignments_unassigned(
            courier.id, scope.admin_user.id, except_order_statuses=TERMINAL_ORDER_STATUSES
        )
        self._commit()

    def generate_access(self, scope: AdminScope, courier_id: uuid.UUID) -> AdminCourierAccessResponse:
        """Sorteia link e codigo, grava os hashes, devolve os dois EM CLARO.

        E a unica vez que o par existe fora do hash. Chamar de novo gera
        outro par e o anterior morre na mesma escrita: e assim que "o
        motoboy saiu, gera outro e o link velho morre na hora" e verdade —
        nao ha sessao nem token derivado que sobreviva a esta linha.

        Inativo nao ganha acesso (409): seria um par que a dependencia
        recusaria de qualquer jeito, e o dono veria "funcionou" numa tela e
        "nao entra" na outra.
        """
        courier = self._get_courier(scope, courier_id)
        if not courier.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Entregador inativo não recebe acesso. Reative-o antes.",
            )
        link_token = generate_courier_link_token()
        access_code = generate_courier_access_code()
        now = utcnow()
        courier.access_link_hash = hash_courier_link_token(link_token)
        courier.access_code_hash = hash_courier_access_code(access_code, link_token)
        courier.access_generated_at = now
        self._commit()
        return AdminCourierAccessResponse(
            courier_id=courier.id,
            link_token=link_token,
            access_code=access_code,
            access_generated_at=now,
        )

    # --- A atribuicao -------------------------------------------------------

    def assign_orders(
        self,
        scope: AdminScope,
        courier_id: uuid.UUID,
        payload: AdminAssignOrdersRequest,
    ) -> AdminAssignmentBatchResponse:
        """Poe um ou mais pedidos nas maos deste entregador.

        Resposta POR ITEM e escrita UMA SO: cada pedido do lote diz `ok` ou
        o motivo, nada levanta no meio, e o commit no fim grava os bons
        juntos. Um pedido de retirada selecionado por engano nao pode
        derrubar os outros quatro.

        A taxa e congelada AQUI, da configuracao da filial sobre a distancia
        que o pedido ja tinha. Reatribuir fecha a linha anterior e abre
        outra com a taxa de agora; atribuir ao mesmo motoboy de novo e no-op.
        """
        courier = self._get_courier(scope, courier_id)
        if not courier.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Entregador inativo não recebe pedido.",
            )
        branch = self._get_branch(scope, courier.branch_id)
        now = utcnow()

        items = []
        for order_id in payload.order_ids:
            items.append(self._assign_one(scope, courier, branch, order_id, now))
        self._commit()
        return AdminAssignmentBatchResponse(items=items)

    def _assign_one(
        self,
        scope: AdminScope,
        courier: Courier,
        branch: Branch,
        order_id: uuid.UUID,
        now,
    ) -> AdminAssignmentResultItem:
        order = self._find_order_in_scope(scope, order_id)
        if order is None:
            return self._rejected(order_id, AssignmentErrorCode.NOT_FOUND)
        if order.order_type != "delivery":
            return self._rejected(order_id, AssignmentErrorCode.NOT_DELIVERY)
        if order.status in TERMINAL_ORDER_STATUSES:
            return self._rejected(order_id, AssignmentErrorCode.ORDER_CLOSED)
        if order.branch_id != courier.branch_id:
            return self._rejected(order_id, AssignmentErrorCode.OTHER_BRANCH)

        current = self.courier_repository.get_open_assignment_of_order(order.id)
        if current is not None and current.courier_id == courier.id:
            return self._accepted(order, current)
        if current is not None:
            self.courier_repository.mark_assignment_unassigned(current, scope.admin_user.id, now)

        assignment = self.courier_repository.create_assignment(
            CourierAssignment(
                order_id=order.id,
                courier_id=courier.id,
                assigned_by_admin_user_id=scope.admin_user.id,
                assigned_at=now,
                courier_fee_snapshot=calculate_courier_fee(
                    branch.courier_fee_base,
                    branch.courier_fee_per_km,
                    order.delivery_distance_km,
                ),
                distance_km_snapshot=order.delivery_distance_km,
            )
        )
        return self._accepted(order, assignment)

    def list_open_assignments(
        self, scope: AdminScope, courier_id: uuid.UUID
    ) -> list[AdminAssignmentResponse]:
        courier = self._get_courier(scope, courier_id)
        rows = self.courier_repository.list_open_orders_by_courier(
            courier.id, exclude_statuses=TERMINAL_ORDER_STATUSES
        )
        return [self._assignment_response(assignment, order) for assignment, order in rows]

    def get_order_courier(self, scope: AdminScope, order_id: uuid.UUID) -> AdminOrderCourierResponse:
        order = self._get_order_in_scope(scope, order_id)
        assignment = self.courier_repository.get_open_assignment_of_order(order.id)
        if assignment is None:
            return AdminOrderCourierResponse()
        courier = self.courier_repository.get_by_id_and_restaurant(
            assignment.courier_id, scope.restaurant_id
        )
        return AdminOrderCourierResponse(
            assignment=self._assignment_response(assignment, order),
            courier=None if courier is None else self._courier_response(courier),
        )

    def unassign_order(self, scope: AdminScope, order_id: uuid.UUID) -> None:
        """Tira o pedido das maos de quem estiver com ele. 409 se ninguem
        estiver: desatribuir o que nao esta atribuido e um clique repetido
        ou uma tela desatualizada, e as duas merecem saber."""
        order = self._get_order_in_scope(scope, order_id)
        assignment = self.courier_repository.get_open_assignment_of_order(order.id)
        if assignment is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Este pedido não está atribuído a nenhum entregador.",
            )
        self.courier_repository.mark_assignment_unassigned(assignment, scope.admin_user.id, utcnow())
        self._commit()

    # --- O relatorio do dono ------------------------------------------------

    def fee_report(
        self,
        *,
        restaurant_id: uuid.UUID,
        start_date: date,
        end_date: date,
        branch_id: uuid.UUID | None,
    ) -> AdminCourierFeeReportResponse:
        """Quanto o dono deve a cada motoboy no periodo.

        Mesmo recorte dos relatorios de Desempenho: datas no fuso da
        operacao, fim exclusivo, teto de `MAX_REPORT_DAYS`. O `branch_id` ja
        chega resolvido pelo escopo (a rota passa por `resolve_branch_filter`
        e `ensure_pode_ler_dinheiro`), e o restaurante e o do token.
        """
        if end_date < start_date:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="end_date não pode ser anterior a start_date"
            )
        if (end_date - start_date).days + 1 > MAX_REPORT_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Período máximo do relatório: {MAX_REPORT_DAYS} dias",
            )
        start_at, end_at = period_bounds(start_date, end_date)
        linhas = self.courier_repository.totals_by_courier(restaurant_id, branch_id, start_at, end_at)

        couriers = [
            AdminCourierFeeReportItem(
                courier_id=linha["courier_id"],
                name=linha["name"],
                phone=linha["phone"],
                branch_id=linha["branch_id"],
                is_deleted=linha["deleted_at"] is not None,
                deliveries_count=int(linha["deliveries_count"]),
                deliveries_without_fee=int(linha["deliveries_without_fee"]),
                fee_total=quantize_money(to_decimal(linha["fee_total"])),
            )
            for linha in linhas
        ]
        return AdminCourierFeeReportResponse(
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            period=ReportPeriod(
                start_date=start_date, end_date=end_date, days=(end_date - start_date).days + 1
            ),
            deliveries_count=sum(item.deliveries_count for item in couriers),
            deliveries_without_fee=sum(item.deliveries_without_fee for item in couriers),
            fee_total=quantize_money(sum((item.fee_total for item in couriers), Decimal("0"))),
            couriers=couriers,
        )

    # --- Escopo -------------------------------------------------------------

    def _find_order_in_scope(self, scope: AdminScope, order_id: uuid.UUID) -> Order | None:
        """O pedido, se este lojista o alcanca — restaurante E filial. `None`
        para os tres casos (nao existe, e de outro restaurante, e da filial
        que ele nao enxerga), porque o lote nao levanta: responde por item."""
        order = self.order_repository.get_order_detail(order_id, scope.restaurant_id)
        if order is None:
            return None
        if not scope.sees_all_branches and order.branch_id != scope.branch_id:
            return None
        return order

    def _get_order_in_scope(self, scope: AdminScope, order_id: uuid.UUID) -> Order:
        order = self._find_order_in_scope(scope, order_id)
        if order is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado")
        return order

    def _get_branch(self, scope: AdminScope, branch_id: uuid.UUID) -> Branch:
        """Filial dentro do escopo do token.

        Duas conferencias, e as duas sao necessarias (o padrao de
        `AdminSettingsService._get_branch`): `ensure_branch_allowed` barra a
        filial que existe mas nao e a deste lojista; o repositorio barra a
        filial de outro restaurante. Mesmo 404 para as duas.
        """
        scope.ensure_branch_allowed(branch_id)
        branch = self.branch_repository.get_active_by_id_and_restaurant(
            branch_id, scope.restaurant_id
        )
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filial não encontrada"
            )
        return branch

    def _get_courier(self, scope: AdminScope, courier_id: uuid.UUID) -> Courier:
        """Entregador dentro do escopo: o repositorio confere o restaurante,
        e a filial so da para conferir depois de ler `courier.branch_id`.
        Mesmo 404 para "nao existe", "e de outro restaurante" e "e da
        filial vizinha"."""
        courier = self.courier_repository.get_by_id_and_restaurant(courier_id, scope.restaurant_id)
        if courier is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ENTREGADOR_NAO_ENCONTRADO)
        if not scope.sees_all_branches and courier.branch_id != scope.branch_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ENTREGADOR_NAO_ENCONTRADO)
        return courier

    def _ensure_phone_is_free(
        self,
        branch_id: uuid.UUID,
        phone: str,
        exclude_courier_id: uuid.UUID | None = None,
    ) -> None:
        if self.courier_repository.exists_phone_in_branch(branch_id, phone, exclude_courier_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=TELEFONE_EM_USO)

    def _commit(self) -> None:
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _rejected(order_id: uuid.UUID, error: AssignmentErrorCode) -> AdminAssignmentResultItem:
        return AdminAssignmentResultItem(order_id=order_id, ok=False, error=error)

    def _accepted(self, order: Order, assignment: CourierAssignment) -> AdminAssignmentResultItem:
        return AdminAssignmentResultItem(
            order_id=order.id, ok=True, assignment=self._assignment_response(assignment, order)
        )

    @staticmethod
    def _assignment_response(assignment: CourierAssignment, order: Order) -> AdminAssignmentResponse:
        return AdminAssignmentResponse(
            id=assignment.id,
            order_id=order.id,
            order_number=order.order_number,
            order_status=order.status,
            courier_id=assignment.courier_id,
            assigned_at=assignment.assigned_at,
            courier_fee_snapshot=(
                None
                if assignment.courier_fee_snapshot is None
                else money_to_float(assignment.courier_fee_snapshot)
            ),
            distance_km_snapshot=(
                None
                if assignment.distance_km_snapshot is None
                else float(assignment.distance_km_snapshot)
            ),
        )

    @staticmethod
    def _courier_response(courier: Courier) -> AdminCourierResponse:
        return AdminCourierResponse(
            id=courier.id,
            branch_id=courier.branch_id,
            name=courier.name,
            phone=courier.phone,
            is_active=bool(courier.is_active),
            has_access=courier.access_link_hash is not None and courier.access_code_hash is not None,
            access_generated_at=courier.access_generated_at,
            created_at=courier.created_at,
        )

    @staticmethod
    def _courier_fee_response(branch: Branch) -> AdminBranchCourierFeeResponse:
        return AdminBranchCourierFeeResponse(
            branch_id=branch.id,
            courier_fee_base=(
                None if branch.courier_fee_base is None else money_to_float(branch.courier_fee_base)
            ),
            courier_fee_per_km=(
                None
                if branch.courier_fee_per_km is None
                else money_to_float(branch.courier_fee_per_km)
            ),
        )
