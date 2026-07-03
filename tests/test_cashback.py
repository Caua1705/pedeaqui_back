import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace

from src.schemas.cashback_schema import CashbackBalanceResponse
from src.services.cashback_service import CashbackService


class FakeCashbackRepository:
    def __init__(self, balance: Decimal):
        self.balance = balance
        self.requested_customer_id = None

    def get_available_balance(self, customer_id):
        self.requested_customer_id = customer_id
        return self.balance


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
