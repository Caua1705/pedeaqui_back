import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.cashback_transaction_model import CashbackTransaction
from src.models.restaurant_model import Restaurant


class CashbackRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_available_balance(self, customer_id: uuid.UUID) -> Decimal:
        """A soma de TODOS os restaurantes. Nao serve para gastar.

        E o numero que `GET /customers/me/cashback` devolve hoje, e ele nao
        e gastavel em lugar nenhum: o saldo pertence a quem o concedeu, e o
        que da para usar numa loja e o do restaurante DELA
        (`get_available_balance_for_restaurant`).

        Enquanto so existe um restaurante os dois numeros coincidem. Quando
        entrar o segundo, esta rota passa a somar saldos que nao sao
        intercambiaveis — e a tela do app muda junto. Ver `docs/cashback.md`,
        secao 4.
        """
        stmt = select(func.coalesce(func.sum(CashbackTransaction.amount), 0)).where(
            CashbackTransaction.customer_id == customer_id,
            CashbackTransaction.status == "available",
        )
        return self.db.scalar(stmt) or Decimal("0.00")

    def get_available_balance_for_restaurant(
        self,
        customer_id: uuid.UUID,
        restaurant_id: uuid.UUID,
    ) -> Decimal:
        """O saldo GASTAVEL naquele restaurante.

        E este que o resgate usa. Cashback de um restaurante gasto em outro
        seria quem concedeu pagando o marketing do concorrente, e nao ha
        mecanismo de compensacao entre eles.

        A soma inclui as linhas negativas (`redeemed`), que e o que faz o
        resgate parcial funcionar sem partir lote nenhum.
        """
        stmt = select(func.coalesce(func.sum(CashbackTransaction.amount), 0)).where(
            CashbackTransaction.customer_id == customer_id,
            CashbackTransaction.restaurant_id == restaurant_id,
            CashbackTransaction.status == "available",
        )
        return self.db.scalar(stmt) or Decimal("0.00")

    def get_by_idempotency_key(self, idempotency_key: str) -> CashbackTransaction | None:
        stmt = select(CashbackTransaction).where(
            CashbackTransaction.idempotency_key == idempotency_key
        )
        return self.db.scalar(stmt)

    def create(self, transaction: CashbackTransaction) -> CashbackTransaction:
        self.db.add(transaction)
        self.db.flush()
        return transaction

    def list_transactions(
        self,
        customer_id: uuid.UUID,
        limit: int,
        offset: int,
    ) -> list[tuple[CashbackTransaction, str | None]]:
        stmt = (
            select(CashbackTransaction, Restaurant.name)
            .outerjoin(Restaurant, Restaurant.id == CashbackTransaction.restaurant_id)
            .where(CashbackTransaction.customer_id == customer_id)
            .order_by(CashbackTransaction.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [(row[0], row[1]) for row in self.db.execute(stmt).all()]
