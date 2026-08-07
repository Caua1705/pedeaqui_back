"""Autenticacao de lojista.

Sem banco da para cobrir a emissao e a validacao do token, a separacao entre
token de cliente e de lojista, e as respostas do login. O que fica para a
Fase 4: o indice unico de e-mail e a query real por lower(email).
"""

import unittest
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from fastapi import HTTPException

from src.schemas.admin_auth_schema import AdminLoginRequest
from src.services import admin_auth_service as admin_auth_module
from src.services.admin_auth_service import ADMIN_TOKEN_PURPOSE, AdminAuthService
from src.utils.security import admin_auth_secret, create_signed_token, hash_password


# bcrypt e caro por design; um hash so, reaproveitado, evita somar segundos
# na suite inteira.
PASSWORD = "senha-de-teste-123"
PASSWORD_HASH = hash_password(PASSWORD)


def make_admin(**overrides):
    values = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "branch_id": None,
        "name": "Junior",
        "email": "junior@exemplo.com",
        "password_hash": PASSWORD_HASH,
        "role": "owner",
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeAdminUserRepository:
    def __init__(self, admin=None):
        self.admin = admin

    def get_by_email(self, email):
        if self.admin and self.admin.email.lower() == email.lower():
            return self.admin
        return None

    def get_by_id(self, admin_user_id):
        if self.admin and self.admin.id == admin_user_id:
            return self.admin
        return None


def build_service(admin=None):
    service = AdminAuthService(SimpleNamespace())
    service.repository = FakeAdminUserRepository(admin)
    return service


class TokenTests(unittest.TestCase):
    def test_token_carries_restaurant_id_and_role(self):
        admin = make_admin()

        token = AdminAuthService.create_access_token(admin)
        payload = jwt.decode(token, admin_auth_secret(), algorithms=["HS256"])

        self.assertEqual(payload["sub"], str(admin.id))
        self.assertEqual(payload["restaurant_id"], str(admin.restaurant_id))
        self.assertEqual(payload["role"], "owner")
        self.assertEqual(payload["type"], "admin")
        self.assertEqual(payload["purpose"], ADMIN_TOKEN_PURPOSE)

    def test_valid_token_resolves_to_the_admin(self):
        admin = make_admin()
        service = build_service(admin)

        resolved = service.get_admin_from_token(AdminAuthService.create_access_token(admin))

        self.assertIs(resolved, admin)

    def test_customer_token_is_not_accepted_as_admin(self):
        # Mesmo segredo por padrao: o que separa os dois e o purpose.
        customer_token = create_signed_token(
            subject=str(uuid.uuid4()),
            purpose="customer_access",
            expires_delta=timedelta(minutes=60),
            extra={"type": "customer"},
            secret=admin_auth_secret(),
        )
        service = build_service(make_admin())

        with self.assertRaises(HTTPException) as raised:
            service.get_admin_from_token(customer_token)

        self.assertEqual(raised.exception.status_code, 401)

    def test_expired_token_is_rejected(self):
        admin = make_admin()
        expired = create_signed_token(
            subject=str(admin.id),
            purpose=ADMIN_TOKEN_PURPOSE,
            expires_delta=timedelta(minutes=-1),
            extra={"type": "admin"},
            secret=admin_auth_secret(),
        )
        service = build_service(admin)

        with self.assertRaises(HTTPException) as raised:
            service.get_admin_from_token(expired)

        self.assertEqual(raised.exception.status_code, 401)

    def test_token_of_a_deleted_admin_is_rejected(self):
        token = AdminAuthService.create_access_token(make_admin())
        service = build_service(None)

        with self.assertRaises(HTTPException) as raised:
            service.get_admin_from_token(token)

        self.assertEqual(raised.exception.status_code, 401)

    def test_deactivated_admin_loses_access_before_the_token_expires(self):
        admin = make_admin()
        token = AdminAuthService.create_access_token(admin)
        service = build_service(admin)
        admin.is_active = False

        with self.assertRaises(HTTPException) as raised:
            service.get_admin_from_token(token)

        self.assertEqual(raised.exception.status_code, 403)

    def test_restaurant_comes_from_the_database_not_from_the_token(self):
        # O restaurant_id do token e informativo. Se o lojista for movido de
        # restaurante, o escopo tem que acompanhar sem esperar o token expirar.
        admin = make_admin()
        token = AdminAuthService.create_access_token(admin)
        service = build_service(admin)
        new_restaurant = uuid.uuid4()
        admin.restaurant_id = new_restaurant

        resolved = service.get_admin_from_token(token)

        self.assertEqual(resolved.restaurant_id, new_restaurant)


class LoginTests(unittest.TestCase):
    def setUp(self):
        # O piso de latencia anti-enumeracao nao precisa custar tempo aqui.
        patcher = patch.object(admin_auth_module, "LOGIN_MIN_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_successful_login_returns_token_and_user(self):
        admin = make_admin()
        service = build_service(admin)

        response = service.login(
            AdminLoginRequest(email=admin.email, password=PASSWORD)
        )

        self.assertEqual(response.token_type, "bearer")
        self.assertEqual(response.admin_user.restaurant_id, admin.restaurant_id)
        self.assertTrue(response.access_token)

    def test_wrong_password_and_unknown_email_are_indistinguishable(self):
        admin = make_admin()

        with self.assertRaises(HTTPException) as wrong_password:
            build_service(admin).login(
                AdminLoginRequest(email=admin.email, password="errada-errada")
            )
        with self.assertRaises(HTTPException) as unknown_email:
            build_service(admin).login(
                AdminLoginRequest(email="ninguem@exemplo.com", password=PASSWORD)
            )

        self.assertEqual(wrong_password.exception.status_code, 401)
        self.assertEqual(unknown_email.exception.status_code, 401)
        self.assertEqual(
            wrong_password.exception.detail, unknown_email.exception.detail
        )

    def test_inactive_user_cannot_log_in(self):
        admin = make_admin(is_active=False)
        service = build_service(admin)

        with self.assertRaises(HTTPException) as raised:
            service.login(AdminLoginRequest(email=admin.email, password=PASSWORD))

        self.assertEqual(raised.exception.status_code, 403)

    def test_email_is_matched_case_insensitively(self):
        admin = make_admin(email="Junior@Exemplo.com")
        service = build_service(admin)

        response = service.login(
            AdminLoginRequest(email="JUNIOR@EXEMPLO.COM", password=PASSWORD)
        )

        self.assertTrue(response.access_token)


class AdminRouteContractTests(unittest.TestCase):
    def test_admin_routes_require_bearer_and_no_longer_accept_api_key(self):
        from main import app

        paths = app.openapi()["paths"]
        protected = [
            ("/admin/orders", "get"),
            ("/admin/orders/status-counts", "get"),
            ("/admin/orders/{order_id}", "get"),
            ("/admin/orders/{order_id}/status", "patch"),
            ("/admin/orders/stream-ticket", "post"),
            ("/admin/coupons", "get"),
            ("/admin/coupons", "post"),
            ("/admin/coupons/{coupon_id}", "patch"),
        ]

        for path, method in protected:
            operation = paths[path][method]
            with self.subTest(path=path, method=method):
                self.assertEqual(operation["security"], [{"HTTPBearer": []}])
                parameter_names = {
                    parameter["name"] for parameter in operation.get("parameters", [])
                }
                self.assertNotIn("x-api-key", parameter_names)
                self.assertNotIn("X-API-Key", parameter_names)

    def test_no_admin_route_takes_the_restaurant_from_the_url(self):
        """A Fase 3 tirou o restaurante do path das rotas /admin.

        O `restaurant_id`/`restaurant_slug` na URL era confrontado com o
        token e nao autorizava nada, mas mantinha na API a forma de um
        parametro que a rota nao pode obedecer. Este teste existe para que a
        proxima rota /admin nao volte a aceita-lo por habito.

        A excecao e o stream: ele nao recebe restaurante nenhum, so um
        ticket assinado de onde o restaurante e derivado.
        """
        from main import app

        admin_paths = [path for path in app.openapi()["paths"] if path.startswith("/admin")]

        for path in admin_paths:
            with self.subTest(path=path):
                self.assertNotIn("{restaurant_id}", path)
                self.assertNotIn("{restaurant_slug}", path)

    def test_login_route_is_public(self):
        from main import app

        operation = app.openapi()["paths"]["/admin/auth/login"]["post"]
        self.assertNotIn("security", operation)

    def test_create_order_documents_the_idempotency_key_header(self):
        from main import app

        operation = app.openapi()["paths"]["/restaurants/{restaurant_slug}/orders"]["post"]
        headers = {
            parameter["name"]
            for parameter in operation["parameters"]
            if parameter["in"] == "header"
        }
        self.assertIn("Idempotency-Key", headers)


if __name__ == "__main__":
    unittest.main()
