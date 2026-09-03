"""Consultas de entregadores e de atribuicoes.

`couriers` tem `restaurant_id` proprio (como `orders`), entao o WHERE do
restaurante nao precisa de juncao. `courier_assignments` nao tem: chega ao
restaurante e a filial pela juncao com `orders`, que e quem sabe de onde o
pedido e.

Excluido (`deleted_at` preenchido) nao existe para nenhuma consulta daqui,
exceto as de historico — a corrida de um motoboy que saiu continua sendo
uma corrida que o dono pagou.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.models.courier_model import Courier, CourierAssignment
from src.models.order_model import Order
from src.models.order_status_history_model import OrderStatusHistory


class CourierRepository:
    def __init__(self, db: Session):
        self.db = db

    # --- Cadastro -----------------------------------------------------------

    def list_by_restaurant(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None = None,
    ) -> list[Courier]:
        """Os entregadores nao excluidos, inativos inclusive: quem desligou
        um precisa continuar vendo-o para religar."""
        conditions = [Courier.restaurant_id == restaurant_id, Courier.deleted_at.is_(None)]
        if branch_id is not None:
            conditions.append(Courier.branch_id == branch_id)
        stmt = select(Courier).where(*conditions).order_by(Courier.name.asc(), Courier.id.asc())
        return list(self.db.scalars(stmt).all())

    def get_by_id_and_restaurant(
        self, courier_id: uuid.UUID, restaurant_id: uuid.UUID
    ) -> Courier | None:
        # restaurant_id obrigatorio pelo mesmo motivo de
        # OrderRepository.get_order_detail: sem ele qualquer lojista com o
        # UUID em maos alcanca o motoboy de outro restaurante.
        stmt = select(Courier).where(
            Courier.id == courier_id,
            Courier.restaurant_id == restaurant_id,
            Courier.deleted_at.is_(None),
        )
        return self.db.scalar(stmt)

    def get_by_link_hash(self, link_hash: str) -> Courier | None:
        """O cadastro que um link abre. Sem filtro de restaurante de
        proposito: o link E a identidade, e nao chega junto de um tenant."""
        stmt = select(Courier).where(
            Courier.access_link_hash == link_hash,
            Courier.deleted_at.is_(None),
        )
        return self.db.scalar(stmt)

    def exists_phone_in_branch(
        self,
        branch_id: uuid.UUID,
        phone: str,
        exclude_courier_id: uuid.UUID | None = None,
    ) -> bool:
        """A mesma pergunta do indice parcial `ux_couriers_branch_phone_ativos`,
        feita antes do INSERT para virar 409 em vez de IntegrityError."""
        conditions = [
            Courier.branch_id == branch_id,
            Courier.phone == phone,
            Courier.deleted_at.is_(None),
        ]
        if exclude_courier_id is not None:
            conditions.append(Courier.id != exclude_courier_id)
        stmt = select(func.count()).select_from(Courier).where(*conditions)
        return (self.db.scalar(stmt) or 0) > 0

    def create(self, courier: Courier) -> Courier:
        self.db.add(courier)
        self.db.flush()
        return courier

    # --- Atribuicoes --------------------------------------------------------

    def get_open_assignment_of_order(self, order_id: uuid.UUID) -> CourierAssignment | None:
        stmt = select(CourierAssignment).where(
            CourierAssignment.order_id == order_id,
            CourierAssignment.unassigned_at.is_(None),
        )
        return self.db.scalar(stmt)

    def create_assignment(self, assignment: CourierAssignment) -> CourierAssignment:
        self.db.add(assignment)
        self.db.flush()
        return assignment

    def mark_assignment_unassigned(
        self,
        assignment: CourierAssignment,
        admin_user_id: uuid.UUID | None,
        now: datetime,
    ) -> CourierAssignment:
        assignment.unassigned_at = now
        assignment.unassigned_by_admin_user_id = admin_user_id
        self.db.add(assignment)
        self.db.flush()
        return assignment

    def mark_open_assignments_unassigned(
        self,
        courier_id: uuid.UUID,
        admin_user_id: uuid.UUID | None,
        now: datetime | None = None,
        except_order_statuses: tuple[str, ...] = (),
    ) -> int:
        """Fecha toda corrida aberta do entregador. Devolve quantas fechou.

        E o que desativar e excluir chamam: um pedido que ficasse com um
        motoboy que nao consegue mais marcar nada ficaria preso, e o painel
        so o veria como "atribuido".

        `except_order_statuses` sao os TERMINAIS, passados por quem sabe o
        que e terminal (a maquina de estados): a corrida de um pedido ja
        entregue nao e "aberta" no sentido de operacao — ela e o registro de
        quem entregou, e fecha-la apagaria a entrega do historico que o dono
        usa para pagar.
        """
        conditions = [
            CourierAssignment.courier_id == courier_id,
            CourierAssignment.unassigned_at.is_(None),
        ]
        if except_order_statuses:
            conditions.append(
                CourierAssignment.order_id.in_(
                    select(Order.id).where(Order.status.notin_(except_order_statuses))
                )
            )
        stmt = (
            update(CourierAssignment)
            .where(*conditions)
            .values(
                unassigned_at=now if now is not None else func.now(),
                unassigned_by_admin_user_id=admin_user_id,
            )
        )
        return self.db.execute(stmt).rowcount

    def list_open_orders_by_courier(
        self,
        courier_id: uuid.UUID,
        exclude_statuses: tuple[str, ...] = (),
    ) -> list[tuple[CourierAssignment, Order]]:
        """As corridas abertas de UM entregador, com o pedido, do mais antigo
        ao mais novo — a ordem em que ele deveria sair.

        `courier_id` e o recorte inteiro: nao ha filtro de restaurante nem
        de filial porque o entregador nao passa nenhum dos dois — a
        atribuicao ja pertence a um pedido de uma loja so.

        `exclude_statuses` sao os terminais, por parametro: a atribuicao de
        um pedido entregue continua aberta (e o registro de quem entregou),
        e quem sabe o que e terminal e a maquina de estados.
        """
        conditions = [
            CourierAssignment.courier_id == courier_id,
            CourierAssignment.unassigned_at.is_(None),
        ]
        if exclude_statuses:
            conditions.append(Order.status.notin_(exclude_statuses))
        stmt = (
            select(CourierAssignment, Order)
            .join(Order, Order.id == CourierAssignment.order_id)
            .where(*conditions)
            .order_by(CourierAssignment.assigned_at.asc(), CourierAssignment.id.asc())
        )
        return list(self.db.execute(stmt).tuples().all())

    def list_deliveries_by_courier(
        self,
        courier_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
    ) -> list[tuple[CourierAssignment, Order, datetime]]:
        """As entregas CONCLUIDAS do entregador no periodo, com o instante.

        O instante e o da linha `completed` em `order_status_history` — e
        nao `orders.updated_at`, que muda por qualquer escrita. `completed`
        e terminal, entao ha no maximo uma linha dessas por pedido, e a
        juncao nao duplica.

        Vale a atribuicao ABERTA (`unassigned_at IS NULL`): e a de quem
        estava com o pedido quando ele foi entregue. A de um motoboy que
        foi trocado antes esta fechada e nao conta — a corrida foi do outro.
        `end_at` e exclusivo, como nos relatorios do painel.
        """
        stmt = (
            select(CourierAssignment, Order, OrderStatusHistory.created_at)
            .join(Order, Order.id == CourierAssignment.order_id)
            .join(
                OrderStatusHistory,
                (OrderStatusHistory.order_id == Order.id)
                & (OrderStatusHistory.status == "completed"),
            )
            .where(
                CourierAssignment.courier_id == courier_id,
                CourierAssignment.unassigned_at.is_(None),
                Order.status == "completed",
                OrderStatusHistory.created_at >= start_at,
                OrderStatusHistory.created_at < end_at,
            )
            .order_by(OrderStatusHistory.created_at.asc(), Order.id.asc())
        )
        return list(self.db.execute(stmt).tuples().all())

    def get_open_order_of_courier(
        self, courier_id: uuid.UUID, order_id: uuid.UUID
    ) -> tuple[CourierAssignment, Order] | None:
        stmt = (
            select(CourierAssignment, Order)
            .join(Order, Order.id == CourierAssignment.order_id)
            .where(
                CourierAssignment.courier_id == courier_id,
                CourierAssignment.order_id == order_id,
                CourierAssignment.unassigned_at.is_(None),
            )
        )
        row = self.db.execute(stmt).tuples().first()
        return row
