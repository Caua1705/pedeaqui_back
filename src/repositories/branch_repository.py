import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.models.branch_model import Branch
from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_delivery_time_band_model import BranchDeliveryTimeBand
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

    def get_by_id_and_restaurant(self, branch_id: uuid.UUID, restaurant_id: uuid.UUID) -> Branch | None:
        """A filial, ATIVA OU NAO, dentro do restaurante.

        Existe ao lado de `get_active_by_id_and_restaurant` por causa da
        impressao: reimprimir a comanda de um pedido e a operacao mais comum
        do balcao (papel picotou, comanda molhou), e ela nao pode parar de
        funcionar porque a loja foi desativada depois. Com o filtro de
        ativa, a via daquele pedido voltaria a sair com o rodape e a
        contagem de vias de ANTES de as colunas existirem — mudando sozinha,
        sem ninguem ter editado nada.

        Nao serve para configurar nem para vender: quem escreve continua
        passando pela versao ativa, que e onde a filial desativada tem que
        sumir mesmo.
        """
        stmt = select(Branch).where(
            Branch.id == branch_id,
            Branch.restaurant_id == restaurant_id,
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

    def list_delivery_time_bands(self, branch_id: uuid.UUID) -> list[BranchDeliveryTimeBand]:
        """As faixas de prazo da filial, do teto menor para o maior.

        A ORDEM e a regra, e nao apresentacao: vale a primeira faixa cujo
        teto alcanca a distancia. Uma listagem sem `ORDER BY` faria a faixa
        vigente mudar entre duas consultas identicas — o Postgres nao promete
        ordem nenhuma sem ele.
        """
        stmt = (
            select(BranchDeliveryTimeBand)
            .where(BranchDeliveryTimeBand.branch_id == branch_id)
            .order_by(BranchDeliveryTimeBand.max_distance_km.asc())
        )
        return list(self.db.scalars(stmt).all())

    def delete_delivery_time_bands(self, branch_id: uuid.UUID) -> None:
        self.db.execute(
            delete(BranchDeliveryTimeBand).where(
                BranchDeliveryTimeBand.branch_id == branch_id
            )
        )

    def add_delivery_time_bands(
        self,
        bands: list[BranchDeliveryTimeBand],
    ) -> list[BranchDeliveryTimeBand]:
        self.db.add_all(bands)
        self.db.flush()
        return bands

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
