"""O que `GET /customers/me/cashback` devolve.

Sem banco: aqui se testa a MONTAGEM da resposta — o total ao lado da quebra
por restaurante, e a validade de cada loja saindo do último pedido dela. As
consultas em si (o `SUM` por restaurante, o `MAX(created_at)`) são testadas
contra o Postgres em `test_cashback_saldo_por_restaurante_db.py`, porque
dublar uma soma é testar a soma do dublê.
"""

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from src.schemas.cashback_schema import CashbackBalanceResponse
from src.services.cashback_service import CashbackService
from tests import fabricas


SEGUNDA = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def restaurante(nome="Júnior da Picanha", slug="junior-da-picanha"):
    return fabricas.restaurante(name=nome, slug=slug)


def regra(expiry_days=60, enabled=True):
    return SimpleNamespace(
        enabled=enabled,
        default_percent=Decimal("5.00"),
        min_redeem_balance=Decimal("5.00"),
        expiry_days=expiry_days,
        weekdays=[],
    )


class FakeCashbackRepository:
    def __init__(self, balance: Decimal, transactions=None, by_restaurant=None):
        self.balance = balance
        self.transactions = transactions or []
        self.by_restaurant = by_restaurant or []
        self.requested_customer_id = None
        self.requested_page = None

    def get_available_balance(self, customer_id):
        self.requested_customer_id = customer_id
        return self.balance

    def list_available_balances_by_restaurant(self, customer_id):
        self.requested_customer_id = customer_id
        return self.by_restaurant

    def list_transactions(self, customer_id, limit, offset):
        self.requested_customer_id = customer_id
        self.requested_page = (limit, offset)
        return self.transactions


class FakeOrderRepository:
    def __init__(self, last_order_at=None):
        self.last_order_at = last_order_at or {}
        self.requested_statuses = None

    def last_order_at_by_restaurant(self, customer_id, statuses):
        self.requested_statuses = statuses
        return self.last_order_at


class FakeCashbackRuleRepository:
    def __init__(self, rules=None):
        self.rules = rules or {}
        self.requested_ids = None

    def list_restaurant_rules(self, restaurant_ids):
        self.requested_ids = restaurant_ids
        return self.rules


def montar_service(repository, order_repository=None, rule_repository=None):
    service = CashbackService(SimpleNamespace())
    service.cashback_repository = repository
    service.order_repository = order_repository or FakeOrderRepository()
    service.cashback_rule_repository = rule_repository or FakeCashbackRuleRepository()
    return service


class CashbackServiceTests(unittest.TestCase):
    def test_returns_zero_when_customer_has_no_transactions(self):
        customer = SimpleNamespace(id=uuid.uuid4())
        repository = FakeCashbackRepository(Decimal("0.00"))
        service = montar_service(repository)

        result = service.get_balance(customer)

        self.assertEqual(
            result,
            CashbackBalanceResponse(balance=0.0, currency="BRL", by_restaurant=[]),
        )
        self.assertEqual(repository.requested_customer_id, customer.id)

    def test_returns_available_balance_for_token_customer(self):
        customer = SimpleNamespace(id=uuid.uuid4())
        loja = restaurante()
        repository = FakeCashbackRepository(
            Decimal("12.34"), by_restaurant=[(loja, Decimal("12.34"))]
        )
        service = montar_service(repository)

        result = service.get_balance(customer)

        self.assertEqual(result.balance, 12.34)
        self.assertEqual(result.currency, "BRL")

    def test_o_total_continua_somando_e_a_quebra_diz_onde_gastar(self):
        """As duas coisas na mesma resposta, e elas respondem perguntas
        diferentes.

        O total é o acumulado — o que a pessoa perde ao excluir a conta. O
        gastável é sempre o de UMA loja: cashback é dinheiro de quem o
        concedeu, e não há compensação entre restaurantes.
        """
        customer = SimpleNamespace(id=uuid.uuid4())
        junior = restaurante("Júnior da Picanha", "junior-da-picanha")
        varjota = restaurante("Varjota Burger", "varjota-burger")
        repository = FakeCashbackRepository(
            Decimal("42.50"),
            by_restaurant=[(junior, Decimal("40.00")), (varjota, Decimal("2.50"))],
        )
        service = montar_service(repository)

        result = service.get_balance(customer)

        self.assertEqual(result.balance, 42.50)
        self.assertEqual([linha.balance for linha in result.by_restaurant], [40.0, 2.5])
        self.assertEqual(result.by_restaurant[0].restaurant_id, junior.id)
        self.assertEqual(result.by_restaurant[0].restaurant_name, "Júnior da Picanha")
        self.assertEqual(result.by_restaurant[0].restaurant_slug, "junior-da-picanha")

    def test_a_validade_de_cada_loja_sai_do_ultimo_pedido_naquela_loja(self):
        """Duas lojas, dois relógios.

        É o que o `by_restaurant[]` existe para conseguir dizer — uma data só
        para os dois saldos estaria errada para um deles.
        """
        customer = SimpleNamespace(id=uuid.uuid4())
        junior = restaurante("Júnior da Picanha", "junior-da-picanha")
        varjota = restaurante("Varjota Burger", "varjota-burger")
        repository = FakeCashbackRepository(
            Decimal("42.50"),
            by_restaurant=[(junior, Decimal("40.00")), (varjota, Decimal("2.50"))],
        )
        service = montar_service(
            repository,
            order_repository=FakeOrderRepository(
                {
                    junior.id: SEGUNDA,
                    varjota.id: datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
                }
            ),
            rule_repository=FakeCashbackRuleRepository(
                {junior.id: regra(expiry_days=60), varjota.id: regra(expiry_days=30)}
            ),
        )

        result = service.get_balance(customer)

        self.assertEqual(
            result.by_restaurant[0].expires_at,
            datetime(2026, 10, 23, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(
            result.by_restaurant[1].expires_at,
            datetime(2026, 8, 31, 12, tzinfo=timezone.utc),
        )

    def test_a_validade_conta_o_pedido_que_chegou_a_cozinha(self):
        """O recorte não é escolhido aqui.

        Quem sabe quais pedidos contam é a máquina de estados, e não se cria
        uma quarta definição de "quais pedidos" neste projeto.
        """
        customer = SimpleNamespace(id=uuid.uuid4())
        loja = restaurante()
        order_repository = FakeOrderRepository({loja.id: SEGUNDA})
        service = montar_service(
            FakeCashbackRepository(
                Decimal("40.00"), by_restaurant=[(loja, Decimal("40.00"))]
            ),
            order_repository=order_repository,
            rule_repository=FakeCashbackRuleRepository({loja.id: regra()}),
        )

        service.get_balance(customer)

        self.assertEqual(
            order_repository.requested_statuses,
            ("accepted", "preparing", "ready", "out_for_delivery", "completed"),
        )

    def test_loja_sem_campanha_configurada_devolve_validade_nula(self):
        """Sem regra não há prazo, e nulo é "não vence".

        Apagar saldo por ausência de configuração seria o lado errado do
        erro — o mesmo motivo pelo qual falta de regra também não gera
        cashback.
        """
        customer = SimpleNamespace(id=uuid.uuid4())
        loja = restaurante()
        service = montar_service(
            FakeCashbackRepository(
                Decimal("40.00"), by_restaurant=[(loja, Decimal("40.00"))]
            ),
            order_repository=FakeOrderRepository({loja.id: SEGUNDA}),
            rule_repository=FakeCashbackRuleRepository({}),
        )

        result = service.get_balance(customer)

        self.assertIsNone(result.by_restaurant[0].expires_at)

    def test_saldo_zerado_nao_consulta_pedido_nem_regra(self):
        """A maioria dos clientes não tem saldo nenhum, e essa é a tela que o
        app abre. Duas consultas a menos para devolver uma lista vazia."""
        customer = SimpleNamespace(id=uuid.uuid4())
        order_repository = FakeOrderRepository()
        rule_repository = FakeCashbackRuleRepository()
        service = montar_service(
            FakeCashbackRepository(Decimal("0.00")),
            order_repository=order_repository,
            rule_repository=rule_repository,
        )

        result = service.get_balance(customer)

        self.assertEqual(result.by_restaurant, [])
        self.assertIsNone(order_repository.requested_statuses)
        self.assertIsNone(rule_repository.requested_ids)

    def test_lists_transactions_with_generated_description_and_numeric_amount(self):
        customer = SimpleNamespace(id=uuid.uuid4())
        transaction = SimpleNamespace(
            id=uuid.uuid4(),
            type="earned",
            amount=Decimal("10.00"),
            status="available",
            order_id=uuid.uuid4(),
            expires_at=None,
            created_at=datetime(2026, 7, 3, 12, tzinfo=timezone.utc),
        )
        repository = FakeCashbackRepository(
            Decimal("13.99"), [(transaction, "Júnior da Picanha")]
        )
        service = montar_service(repository)

        result = service.list_transactions(customer, limit=20, offset=0)

        self.assertEqual(result.balance, 13.99)
        self.assertEqual(repository.requested_customer_id, customer.id)
        self.assertEqual(repository.requested_page, (20, 0))
        self.assertEqual(result.transactions[0].description, "Cashback recebido")
        self.assertEqual(result.transactions[0].restaurant_name, "Júnior da Picanha")
        self.assertEqual(result.transactions[0].amount, 10.0)

    def test_o_extrato_nao_paga_a_quebra_por_restaurante(self):
        """`CashbackTransactionsResponse` não herda de `CashbackBalanceResponse`
        de propósito: a tela do extrato não mostra a lista, e herdando ela
        pagaria as três consultas para montá-la."""
        customer = SimpleNamespace(id=uuid.uuid4())
        order_repository = FakeOrderRepository()
        service = montar_service(
            FakeCashbackRepository(Decimal("13.99")),
            order_repository=order_repository,
        )

        result = service.list_transactions(customer, limit=20, offset=0)

        self.assertFalse(hasattr(result, "by_restaurant"))
        self.assertIsNone(order_repository.requested_statuses)

    def test_lists_empty_transactions(self):
        customer = SimpleNamespace(id=uuid.uuid4())
        repository = FakeCashbackRepository(Decimal("0.00"))
        service = montar_service(repository)

        result = service.list_transactions(customer, limit=50, offset=10)

        self.assertEqual(result.balance, 0.0)
        self.assertEqual(result.transactions, [])
