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

        Sobrou para dois usos, e os dois somam de proposito:

        - o `balance` de `GET /customers/me/cashback`, que continua sendo o
          ACUMULADO. O que da para gastar numa loja vai ao lado, em
          `by_restaurant[]` (`list_available_balances_by_restaurant`), e e o
          que a tela mostra;
        - o que a pessoa PERDE ao excluir a conta — ai a soma e a resposta
          certa, porque a anonimizacao leva o saldo de todos os restaurantes
          de uma vez.

        Para decidir quanto entra num pedido, nunca este:
        `get_available_balance_for_restaurant`.
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

    def list_available_balances_by_restaurant(
        self,
        customer_id: uuid.UUID,
    ) -> list[tuple[Restaurant, Decimal]]:
        """O saldo de cada restaurante em que ainda ha dinheiro, com a loja.

        Uma consulta e nao uma por restaurante: a tela do app abre com isto,
        e o numero de restaurantes em que a pessoa acumulou nao tem teto.

        **`HAVING > 0` e o filtro que importa.** O razao e assinado — o
        resgate entra negativo —, entao restaurante ja gasto ate o fim soma
        zero, e uma linha de saldo zero na tela nao ajuda ninguem. Soma
        negativa nao deveria existir (o resgate nunca passa do saldo), e se
        existir e um bug: publica-la faria a tela mostrar divida.

        **`JOIN` e nao `OUTER JOIN`, e isso descarta linha orfa de
        proposito.** `cashback_transactions.restaurant_id` e nullable com
        `ON DELETE SET NULL`: restaurante apagado deixa saldo sem loja, que
        nao tem nome para mostrar nem cardapio onde gastar. Ele continua no
        `balance` total — que e o acumulado, e o extrato continua listando a
        linha.
        """
        saldo = func.sum(CashbackTransaction.amount)
        stmt = (
            select(Restaurant, saldo)
            .select_from(CashbackTransaction)
            .join(Restaurant, Restaurant.id == CashbackTransaction.restaurant_id)
            .where(
                CashbackTransaction.customer_id == customer_id,
                CashbackTransaction.status == "available",
            )
            .group_by(Restaurant.id)
            .having(saldo > 0)
            # Maior saldo primeiro, e o nome so desempata: sem a segunda
            # chave, dois restaurantes com o mesmo saldo trocam de lugar
            # entre uma abertura da tela e a seguinte.
            .order_by(saldo.desc(), Restaurant.name)
        )
        return [(row[0], row[1]) for row in self.db.execute(stmt).all()]

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
