import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.branch_model import Branch
from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_payment_method_model import BranchPaymentMethod


class BranchRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_active_by_id_and_restaurant(self, branch_id: uuid.UUID, restaurant_id: uuid.UUID) -> Branch | None:
        stmt = select(Branch).where(
            Branch.id == branch_id,
            Branch.restaurant_id == restaurant_id,
            Branch.is_active.is_(True),
        )
        return self.db.scalar(stmt)

    def list_active_by_restaurant(self, restaurant_id: uuid.UUID) -> list[Branch]:
        stmt = (
            select(Branch)
            .where(Branch.restaurant_id == restaurant_id, Branch.is_active.is_(True))
            .order_by(Branch.is_main.desc().nulls_last(), Branch.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_default_branch(self, restaurant_id: uuid.UUID) -> Branch | None:
        """A filial a usar quando o cliente nao escolheu nenhuma.

        UMA definicao para a plataforma inteira. Antes havia duas: o
        `/restaurants/{slug}/info` pegava `list_active_by_restaurant()[0]` e a
        estimativa de entrega exigia `is_main`, recusando com 400 o
        restaurante que nao tivesse a flag marcada. Como a ordenacao daquela
        listagem ja e `is_main DESC NULLS LAST, name ASC`, os dois
        concordavam quando havia filial principal e discordavam exatamente
        quando ela faltava — o caso em que uma rota respondia e a outra
        falhava, para o mesmo restaurante, no mesmo minuto.

        A regra que fica e a mais permissiva das duas: principal se houver,
        senao a primeira ativa em ordem alfabetica. Devolver `None` (e nao
        levantar) e de proposito — cada rota tem o proprio codigo de erro
        para "restaurante sem filial", e centraliza-lo aqui mudaria contrato
        publicado.
        """
        branches = self.list_active_by_restaurant(restaurant_id)
        return branches[0] if branches else None

    def list_business_hours(self, branch_id: uuid.UUID) -> list[BranchBusinessHour]:
        stmt = (
            select(BranchBusinessHour)
            .where(BranchBusinessHour.branch_id == branch_id)
            .order_by(
                BranchBusinessHour.weekday.asc(),
                BranchBusinessHour.sort_order.asc(),
                BranchBusinessHour.id.asc(),
            )
        )
        return list(self.db.scalars(stmt).all())

    def list_business_hours_by_weekday(
        self,
        branch_id: uuid.UUID,
        weekday: int,
    ) -> list[BranchBusinessHour]:
        stmt = (
            select(BranchBusinessHour)
            .where(
                BranchBusinessHour.branch_id == branch_id,
                BranchBusinessHour.weekday == weekday,
            )
            .order_by(
                BranchBusinessHour.sort_order.asc(),
                BranchBusinessHour.id.asc(),
            )
        )
        return list(self.db.scalars(stmt).all())

    def list_enabled_payment_methods(
        self, branch_id: uuid.UUID
    ) -> list[BranchPaymentMethod]:
        stmt = (
            select(BranchPaymentMethod)
            .where(
                BranchPaymentMethod.branch_id == branch_id,
                BranchPaymentMethod.enabled.is_(True),
            )
            .order_by(
                BranchPaymentMethod.payment_flow.asc(),
                BranchPaymentMethod.sort_order.asc(),
                BranchPaymentMethod.id.asc(),
            )
        )
        return list(self.db.scalars(stmt).all())
