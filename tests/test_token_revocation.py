"""Revogacao de JWT na troca de senha.

O token de cliente vale 7 dias (CUSTOMER_ACCESS_TOKEN_MINUTES). Antes desta
mudanca, trocar a senha nao fazia nada com os tokens ja emitidos: quem
tivesse roubado um seguia dentro da conta pela semana inteira, e a vitima
nao tinha como expulsa-lo.
"""

import unittest
from datetime import timedelta
from types import SimpleNamespace

from src.services.auth_service import AuthService
from src.utils.security import create_signed_token, utcnow
from tests import fabricas


def build_service(customer):
    service = AuthService.__new__(AuthService)
    service.customer_repository = SimpleNamespace(get_by_id=lambda customer_id: customer)
    return service


def make_customer(password_changed_at=None):
    return fabricas.cliente(password_changed_at=password_changed_at)


def issue_token(customer, issued_at=None):
    """Emite um token como o login emite, opcionalmente datado no passado."""
    token = create_signed_token(
        subject=str(customer.id),
        purpose="customer_access",
        expires_delta=timedelta(days=7),
        extra={"type": "customer"},
    )
    if issued_at is None:
        return token
    # PyJWT so aceita `iat` pelo payload; para simular um token antigo,
    # reemitimos com o relogio deslocado.
    import jwt

    from src.core.config import settings

    payload = jwt.decode(
        token,
        settings.CUSTOMER_AUTH_SECRET,
        algorithms=["HS256"],
    )
    payload["iat"] = int(issued_at.timestamp())
    return jwt.encode(payload, settings.CUSTOMER_AUTH_SECRET, algorithm="HS256")


class RevocationTests(unittest.TestCase):
    def test_token_issued_before_the_password_change_is_refused(self):
        customer = make_customer(password_changed_at=utcnow())
        token = issue_token(customer, issued_at=utcnow() - timedelta(hours=2))

        self.assertIsNone(build_service(customer).get_customer_from_token(token))

    def test_token_issued_after_the_password_change_still_works(self):
        customer = make_customer(password_changed_at=utcnow() - timedelta(hours=2))
        token = issue_token(customer)

        self.assertIs(build_service(customer).get_customer_from_token(token), customer)

    def test_account_that_never_changed_the_password_is_untouched(self):
        customer = make_customer(password_changed_at=None)
        token = issue_token(customer, issued_at=utcnow() - timedelta(days=3))

        self.assertIs(build_service(customer).get_customer_from_token(token), customer)

    def test_naive_timestamp_from_the_database_is_treated_as_utc(self):
        # Defesa contra driver/coluna devolvendo datetime sem tzinfo: sem
        # isso a comparacao levanta TypeError e derruba TODA requisicao
        # autenticada.
        changed_at = utcnow().replace(tzinfo=None)
        customer = make_customer(password_changed_at=changed_at)
        token = issue_token(customer, issued_at=utcnow() - timedelta(hours=2))

        self.assertIsNone(build_service(customer).get_customer_from_token(token))


class PasswordChangeMarksTheClockTests(unittest.TestCase):
    def test_change_password_writes_password_changed_at(self):
        from src.schemas.customer_schema import ChangeCustomerPasswordRequest
        from src.services.customer_service import CustomerService
        from src.utils.security import hash_password

        customer = fabricas.cliente(password_hash=hash_password("senha-antiga"))
        service = CustomerService.__new__(CustomerService)
        service.db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)

        service.change_password(
            customer,
            ChangeCustomerPasswordRequest(
                current_password="senha-antiga",
                new_password="senha-nova-123",
                confirm_password="senha-nova-123",
            ),
        )

        self.assertIsNotNone(customer.password_changed_at)


if __name__ == "__main__":
    unittest.main()
