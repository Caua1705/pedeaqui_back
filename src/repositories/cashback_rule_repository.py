import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from src.models.cashback_rule_model import CashbackRule


class CashbackRuleRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_rules_for_branch(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID,
    ) -> tuple[CashbackRule | None, CashbackRule | None]:
        """A regra da filial e a do restaurante, nessa ordem.

        As duas numa consulta so, e nao uma por linha: sao no maximo dois
        registros, e o caminho que chama isto e o do checkout — onde toda ida
        ao banco a mais custa em cima do pedido do cliente.

        `selectinload` nos dias da semana pelo mesmo motivo. Sem ele, ler
        `regra.weekdays` dispara um SELECT por regra na hora em que
        `resolve_cashback_terms` procura o percentual do dia, e o N+1 aparece
        justamente na rota mais quente.

        Quem decide qual das duas vale e `resolve_cashback_terms`, nao este
        repositorio: aqui so se consulta.
        """
        stmt = (
            select(CashbackRule)
            .options(selectinload(CashbackRule.weekdays))
            .where(
                CashbackRule.restaurant_id == restaurant_id,
                or_(
                    CashbackRule.branch_id == branch_id,
                    CashbackRule.branch_id.is_(None),
                ),
            )
        )
        regras = list(self.db.scalars(stmt))
        da_filial = next((regra for regra in regras if regra.branch_id is not None), None)
        do_restaurante = next((regra for regra in regras if regra.branch_id is None), None)
        return da_filial, do_restaurante

    def list_restaurant_rules(
        self,
        restaurant_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, CashbackRule]:
        """A regra do RESTAURANTE (`branch_id IS NULL`) de cada um deles.

        Sem regra de filial nenhuma, e nao e esquecimento: quem chama isto e
        a tela de saldo, e o saldo e do restaurante inteiro. A validade dele
        so pode sair de uma regra que valha para o restaurante inteiro —
        duas filiais com prazos diferentes dariam duas respostas para
        "quando este saldo vence", e o saldo e um so.

        Uma consulta para a lista toda; o `selectinload` esta aqui pelo mesmo
        motivo de `get_rules_for_branch`.
        """
        if not restaurant_ids:
            return {}
        stmt = (
            select(CashbackRule)
            .options(selectinload(CashbackRule.weekdays))
            .where(
                CashbackRule.restaurant_id.in_(restaurant_ids),
                CashbackRule.branch_id.is_(None),
            )
        )
        return {regra.restaurant_id: regra for regra in self.db.scalars(stmt)}
