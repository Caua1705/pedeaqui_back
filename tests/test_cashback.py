import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from src.schemas.cashback_schema import CashbackBalanceResponse
from src.services.cashback_service import CashbackService


class FakeCashbackRepository:
    def __init__(self, balance: Decimal, transactions=None):
        self.balance = balance
        self.transactions = transactions or []
        self.requested_customer_id = None
        self.requested_page = None

    def get_available_balance(self, customer_id):
        self.requested_customer_id = customer_id
        return self.balance

    def list_transactions(self, customer_id, limit, offset):
        self.requested_customer_id = customer_id
        self.requested_page = (limit, offset)
        return self.transactions


class CashbackServiceTests(unittest.TestCase):
    def test_returns_zero_when_customer_has_no_transactions(self):
        customer = SimpleNamespace(id=uuid.uuid4())
        repository = FakeCashbackRepository(Decimal("0.00"))
        service = CashbackService(SimpleNamespace())
        service.cashback_repository = repository

        result = service.get_balance(customer)

        self.assertEqual(result, CashbackBalanceResponse(balance=0.0, currency="BRL"))
        self.assertEqual(repository.requested_customer_id, customer.id)

    def test_returns_available_balance_for_token_customer(self):
        customer = SimpleNamespace(id=uuid.uuid4())
        repository = FakeCashbackRepository(Decimal("12.34"))
        service = CashbackService(SimpleNamespace())
        service.cashback_repository = repository

        result = service.get_balance(customer)

        self.assertEqual(result.balance, 12.34)
        self.assertEqual(result.currency, "BRL")

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
        service = CashbackService(SimpleNamespace())
        service.cashback_repository = repository

        result = service.list_transactions(customer, limit=20, offset=0)

        self.assertEqual(result.balance, 13.99)
        self.assertEqual(repository.requested_customer_id, customer.id)
        self.assertEqual(repository.requested_page, (20, 0))
        self.assertEqual(result.transactions[0].description, "Cashback recebido")
        self.assertEqual(result.transactions[0].restaurant_name, "Júnior da Picanha")
        self.assertEqual(result.transactions[0].amount, 10.0)

    def test_lists_empty_transactions(self):
        customer = SimpleNamespace(id=uuid.uuid4())
        repository = FakeCashbackRepository(Decimal("0.00"))
        service = CashbackService(SimpleNamespace())
        service.cashback_repository = repository

        result = service.list_transactions(customer, limit=50, offset=10)

        self.assertEqual(result.balance, 0.0)
        self.assertEqual(result.transactions, [])
