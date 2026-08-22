"""Testes de idempotencia.

Cobertura possivel sem banco: a maquina de decisao (o que begin() responde
para cada estado da linha), a construcao do escopo, a canonicalizacao do
fingerprint e o short-circuit dentro do OrderService.

O que NAO da para provar aqui e fica para os testes de integracao da Fase 4:
o INSERT ... ON CONFLICT DO NOTHING de verdade, o bloqueio no indice unico
entre duas transacoes concorrentes, o round-trip do JSONB e a remocao por
expires_at. Esses dependem de um Postgres real — um fake sempre vai
confirmar a expectativa de quem o escreveu.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.api.dependencies.admin_scope import AdminScope
from src.core.config import settings
from src.models.idempotency_key_model import (
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_IN_PROGRESS,
)
from src.schemas.order_schema import (
    CreateOrderRequest,
    CreateOrderResponse,
    OrderDetailResponse,
)
from src.services.admin_order_service import AdminOrderService
from src.services.idempotency_service import (
    IdempotencyService,
    normalize_idempotency_key,
)
from src.services.order_service import OrderService


NOW = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)


def make_record(**overrides):
    values = {
        "id": uuid.uuid4(),
        "key": "key-1",
        "scope": "scope-1",
        "request_fingerprint": "fp-1",
        "status": IDEMPOTENCY_COMPLETED,
        "response_body": {"id": str(uuid.uuid4()), "order_number": 42},
        "order_id": uuid.uuid4(),
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=24),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeIdempotencyRepository:
    """Simula (scope, key) unico em memoria.

    `reserve` devolve None quando ja existe linha para o par, que e o que o
    ON CONFLICT DO NOTHING faz quando encontra uma linha ja committed.
    """

    def __init__(self, existing=None):
        self.rows = {}
        if existing is not None:
            self.rows[(existing.scope, existing.key)] = existing
        self.reserve_calls = []
        self.completed = []

    def reserve(self, *, scope, key, request_fingerprint, expires_at):
        self.reserve_calls.append((scope, key, request_fingerprint, expires_at))
        if (scope, key) in self.rows:
            return None
        record = make_record(
            scope=scope,
            key=key,
            request_fingerprint=request_fingerprint,
            status=IDEMPOTENCY_IN_PROGRESS,
            response_body=None,
            order_id=None,
            expires_at=expires_at,
        )
        self.rows[(scope, key)] = record
        return record.id

    def get(self, *, scope, key):
        return self.rows.get((scope, key))

    def complete(self, *, record_id, response_body, order_id):
        self.completed.append((record_id, response_body, order_id))
        for record in self.rows.values():
            if record.id == record_id:
                record.status = IDEMPOTENCY_COMPLETED
                record.response_body = response_body
                record.order_id = order_id
                break


class ScopeTests(unittest.TestCase):
    def test_same_key_in_different_restaurants_does_not_collide(self):
        key_args = {"route": "POST /orders", "requester": "phone:85999999999"}
        scope_a = IdempotencyService.build_scope(restaurant_id=uuid.uuid4(), **key_args)
        scope_b = IdempotencyService.build_scope(restaurant_id=uuid.uuid4(), **key_args)
        self.assertNotEqual(scope_a, scope_b)

    def test_same_key_from_different_requesters_does_not_collide(self):
        restaurant_id = uuid.uuid4()
        scope_a = IdempotencyService.build_scope(
            restaurant_id=restaurant_id, route="POST /orders", requester="customer:1"
        )
        scope_b = IdempotencyService.build_scope(
            restaurant_id=restaurant_id, route="POST /orders", requester="customer:2"
        )
        self.assertNotEqual(scope_a, scope_b)

    def test_same_key_on_different_routes_does_not_collide(self):
        restaurant_id = uuid.uuid4()
        scope_a = IdempotencyService.build_scope(
            restaurant_id=restaurant_id, route="POST /orders", requester="customer:1"
        )
        scope_b = IdempotencyService.build_scope(
            restaurant_id=restaurant_id, route="PATCH /orders/status", requester="customer:1"
        )
        self.assertNotEqual(scope_a, scope_b)


class FingerprintTests(unittest.TestCase):
    def test_key_order_does_not_change_the_fingerprint(self):
        self.assertEqual(
            IdempotencyService.fingerprint({"a": 1, "b": 2}),
            IdempotencyService.fingerprint({"b": 2, "a": 1}),
        )

    def test_different_content_changes_the_fingerprint(self):
        self.assertNotEqual(
            IdempotencyService.fingerprint({"quantity": 1}),
            IdempotencyService.fingerprint({"quantity": 2}),
        )


class BeginTests(unittest.TestCase):
    def _service(self, existing=None):
        service = IdempotencyService(SimpleNamespace())
        service.repository = FakeIdempotencyRepository(existing)
        return service

    def test_first_call_reserves_and_lets_the_caller_proceed(self):
        service = self._service()
        result = service.begin(scope="s", key="k", request_fingerprint="fp")
        self.assertIsNone(result)
        self.assertEqual(len(service.repository.reserve_calls), 1)

    def test_missing_key_skips_protection_entirely(self):
        service = self._service()
        result = service.begin(scope="s", key=None, request_fingerprint="fp")
        self.assertIsNone(result)
        self.assertEqual(service.repository.reserve_calls, [])

    def test_completed_with_same_fingerprint_replays_stored_body(self):
        stored = {"order_number": 99, "total": 50.0}
        record = make_record(scope="s", key="k", request_fingerprint="fp", response_body=stored)
        service = self._service(record)

        result = service.begin(scope="s", key="k", request_fingerprint="fp")

        self.assertEqual(result, stored)

    def test_same_key_with_different_body_is_rejected_with_422(self):
        record = make_record(scope="s", key="k", request_fingerprint="fp-original")
        service = self._service(record)

        with self.assertRaises(HTTPException) as raised:
            service.begin(scope="s", key="k", request_fingerprint="fp-diferente")

        self.assertEqual(raised.exception.status_code, 422)

    def test_in_progress_is_rejected_with_409(self):
        record = make_record(
            scope="s",
            key="k",
            request_fingerprint="fp",
            status=IDEMPOTENCY_IN_PROGRESS,
            response_body=None,
        )
        service = self._service(record)

        with self.assertRaises(HTTPException) as raised:
            service.begin(scope="s", key="k", request_fingerprint="fp")

        self.assertEqual(raised.exception.status_code, 409)

    def test_completed_without_stored_body_is_rejected_with_409(self):
        record = make_record(
            scope="s", key="k", request_fingerprint="fp", response_body=None
        )
        service = self._service(record)

        with self.assertRaises(HTTPException) as raised:
            service.begin(scope="s", key="k", request_fingerprint="fp")

        self.assertEqual(raised.exception.status_code, 409)

    def test_complete_is_a_noop_when_nothing_was_reserved(self):
        service = self._service()
        service.complete(response_body={"a": 1}, order_id=uuid.uuid4())
        self.assertEqual(service.repository.completed, [])

    def test_complete_stores_body_against_the_reserved_row(self):
        service = self._service()
        service.begin(scope="s", key="k", request_fingerprint="fp")
        order_id = uuid.uuid4()

        service.complete(response_body={"order_number": 7}, order_id=order_id)

        self.assertEqual(len(service.repository.completed), 1)
        _, body, stored_order_id = service.repository.completed[0]
        self.assertEqual(body, {"order_number": 7})
        self.assertEqual(stored_order_id, order_id)


class StoredResponseCompatibilityTests(unittest.TestCase):
    """Resposta gravada por uma versao anterior da API.

    A Fase 2 acrescentou campos obrigatorios em CreateOrderResponse
    (tracking_token, payment_flow, payment_status). Uma chave criada antes
    do deploy fica ate 24h no banco com a resposta antiga; reenvia-la nao
    pode virar 500 nem, muito pior, criar um segundo pedido.
    """

    def _valid_body(self):
        return {
            "id": str(uuid.uuid4()),
            "order_number": 10,
            "tracking_token": "token",
            "status": "pending",
            "payment_flow": "delivery",
            "payment_status": "on_delivery",
            "subtotal": 50.0,
            "delivery_fee": 0.0,
            "service_fee": 0.0,
            "total": 50.0,
            "message": "Pedido criado com sucesso",
        }

    def test_current_body_is_parsed(self):
        parsed = IdempotencyService.parse_stored_response(
            CreateOrderResponse, self._valid_body()
        )
        self.assertEqual(parsed.order_number, 10)

    def test_body_from_an_older_version_answers_409(self):
        old_body = self._valid_body()
        del old_body["tracking_token"]
        del old_body["payment_flow"]
        del old_body["payment_status"]

        with self.assertRaises(HTTPException) as raised:
            IdempotencyService.parse_stored_response(CreateOrderResponse, old_body)

        self.assertEqual(raised.exception.status_code, 409)


class HeaderPolicyTests(unittest.TestCase):
    def test_absent_header_is_accepted_when_not_required(self):
        with patch.object(settings, "IDEMPOTENCY_REQUIRED", False):
            self.assertIsNone(normalize_idempotency_key(None))
            self.assertIsNone(normalize_idempotency_key("   "))

    def test_absent_header_is_rejected_when_required(self):
        with patch.object(settings, "IDEMPOTENCY_REQUIRED", True):
            with self.assertRaises(HTTPException) as raised:
                normalize_idempotency_key(None)
        self.assertEqual(raised.exception.status_code, 400)

    def test_key_is_trimmed(self):
        self.assertEqual(normalize_idempotency_key("  abc  "), "abc")

    def test_oversized_key_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            normalize_idempotency_key("x" * 201)
        self.assertEqual(raised.exception.status_code, 400)


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def refresh(self, value):
        pass


class FakeOrderRepository:
    def __init__(self, db):
        self.db = db
        self.orders = []

    def create_order(self, order):
        order.id = uuid.uuid4()
        order.order_number = 123
        self.orders.append(order)
        self.db.events.append("order")

    def create_order_items(self, items):
        for item in items:
            item.id = uuid.uuid4()

    def create_order_item_options(self, options):
        pass

    def create_status_history(self, history):
        pass


def build_order_service(db, restaurant_id):
    """OrderService com todas as dependencias de banco trocadas por fakes."""
    branch = SimpleNamespace(
        id=uuid.uuid4(),
        # As tres chaves da operacao sao da FILIAL desde a revisao
        # 20260818_0025; os seis campos nulos sao "herda o padrao do
        # restaurante", que e como toda filial nasce.
        is_open=True,
        accepts_delivery=True,
        accepts_pickup=True,
        delivery_paused_until=None,
        delivery_pause_reason=None,
        min_order_value=None,
        service_fee_enabled=None,
        service_fee_amount=None,
        estimated_delivery_time_min=None,
        estimated_delivery_time_max=None,
        default_delivery_fee=None,
        free_delivery_enabled=None,
        free_delivery_min_order_value=None,
    )
    product_id = uuid.uuid4()
    product = SimpleNamespace(
        id=product_id,
        code="P1",
        catalog_key=None,
        name="Picanha",
        description=None,
        price=Decimal("50.00"),
        option_groups=[],
    )

    service = OrderService(db)
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: SimpleNamespace(id=restaurant_id)
    )
    service.branch_repository = SimpleNamespace(
        get_active_by_id_and_restaurant=lambda branch_id, restaurant: branch,
        # A filial aceita dinheiro na entrega: e o que o payload usa.
        list_enabled_payment_methods=lambda branch: [
            SimpleNamespace(method_type="cash", payment_flow="delivery"),
        ],
    )
    # Filial aberta agora: a validacao de horario e feita pelo
    # BranchHoursService, e o pedido nao chega ao banco sem ela.
    service.branch_hours_service = SimpleNamespace(
        ensure_branch_is_open=lambda branch: None
    )
    service.menu_repository = SimpleNamespace(
        get_settings=lambda restaurant: SimpleNamespace(
            min_order_value=Decimal("0"),
            service_fee_enabled=False,
            service_fee_amount=Decimal("0"),
            estimated_delivery_time_min=None,
            estimated_delivery_time_max=None,
            default_delivery_fee=None,
            free_delivery_enabled=None,
            free_delivery_min_order_value=None,
            platform_commission_percent=Decimal("10.00"),
        )
    )
    service.product_repository = SimpleNamespace(
        list_active_by_ids=lambda restaurant, ids: [product]
    )
    service.order_repository = FakeOrderRepository(db)
    service.idempotency_service.repository = FakeIdempotencyRepository()

    payload = CreateOrderRequest.model_validate({
        "branch_id": str(branch.id),
        "order_type": "pickup",
        "payment_method": "cash",
        "customer": {"name": "Caua", "phone": "85999999999"},
        "items": [{"product_id": str(product_id), "quantity": 1}],
    })
    return service, payload


class CreateOrderIdempotencyTests(unittest.TestCase):
    def test_resending_the_same_key_does_not_create_a_second_order(self):
        db = FakeDb()
        restaurant_id = uuid.uuid4()
        service, payload = build_order_service(db, restaurant_id)

        first = service.create_order("junior", payload, None, idempotency_key="key-abc")

        # Segunda chamada, mesma chave e mesmo corpo, reaproveitando o estado
        # da tabela de idempotencia — e o reenvio que o app faz quando a
        # resposta se perde.
        second_service, _ = build_order_service(db, restaurant_id)
        second_service.idempotency_service.repository = service.idempotency_service.repository
        second = second_service.create_order("junior", payload, None, idempotency_key="key-abc")

        self.assertEqual(len(service.order_repository.orders), 1)
        self.assertEqual(len(second_service.order_repository.orders), 0)
        self.assertEqual(second.order_number, first.order_number)
        self.assertEqual(second.total, first.total)

    def test_same_key_in_another_restaurant_creates_its_own_order(self):
        db = FakeDb()
        shared_repository = FakeIdempotencyRepository()

        service_a, payload_a = build_order_service(db, uuid.uuid4())
        service_a.idempotency_service.repository = shared_repository
        service_b, payload_b = build_order_service(db, uuid.uuid4())
        service_b.idempotency_service.repository = shared_repository

        service_a.create_order("resta", payload_a, None, idempotency_key="mesma-chave")
        service_b.create_order("restb", payload_b, None, idempotency_key="mesma-chave")

        self.assertEqual(len(service_a.order_repository.orders), 1)
        self.assertEqual(len(service_b.order_repository.orders), 1)

    def test_response_is_stored_before_the_commit(self):
        db = FakeDb()
        service, payload = build_order_service(db, uuid.uuid4())

        service.create_order("junior", payload, None, idempotency_key="key-abc")

        self.assertEqual(len(service.idempotency_service.repository.completed), 1)
        _, body, order_id = service.idempotency_service.repository.completed[0]
        self.assertEqual(body["order_number"], 123)
        self.assertEqual(order_id, service.order_repository.orders[0].id)
        self.assertEqual(db.events, ["order", "commit"])

    def test_order_without_key_is_created_without_protection(self):
        db = FakeDb()
        service, payload = build_order_service(db, uuid.uuid4())

        service.create_order("junior", payload, None, idempotency_key=None)

        self.assertEqual(len(service.order_repository.orders), 1)
        self.assertEqual(service.idempotency_service.repository.reserve_calls, [])


def _admin(admin_id):
    """Lojista autenticado. O service so precisa de id (escopo da chave de
    idempotencia) e e-mail (assinatura no historico)."""
    return SimpleNamespace(id=admin_id, email=f"lojista-{admin_id}@exemplo.com")


def _owner_scope(restaurant_id):
    """Escopo de dono: restaurante do token e todas as filiais."""
    return AdminScope(admin_user=None, restaurant_id=restaurant_id, branch_id=None)


class StatusHistoryRepository:
    """Guarda o historico de status, que e o que duplicava a cada reenvio."""

    def __init__(self, order):
        self.order = order
        self.history = []

    def get_order_detail(self, order_id, restaurant_id):
        if self.order.id != order_id or self.order.restaurant_id != restaurant_id:
            return None
        return self.order

    def update_status(self, order, new_status):
        order.status = new_status

    def create_status_history(self, history):
        self.history.append(history)


class UpdateStatusIdempotencyTests(unittest.TestCase):
    def _service(self, db, order, repository=None):
        service = AdminOrderService(db)
        service.order_repository = StatusHistoryRepository(order)
        service.coupon_service = SimpleNamespace(reverse_for_order=lambda order_id: None)
        service.idempotency_service.repository = repository or FakeIdempotencyRepository()
        return service

    def _order(self, restaurant_id):
        return SimpleNamespace(
            id=uuid.uuid4(),
            restaurant_id=restaurant_id,
            status="pending",
            order_type="delivery",
            payment_status="on_delivery",
            order_number=7,
        )

    def test_resending_the_same_key_does_not_duplicate_status_history(self):
        db = FakeDb()
        restaurant_id = uuid.uuid4()
        admin_id = uuid.uuid4()
        order = self._order(restaurant_id)
        shared_repository = FakeIdempotencyRepository()
        request = SimpleNamespace(status="accepted", note=None)

        detail = {"id": str(order.id), "status": "accepted"}
        with patch.object(
            OrderService, "to_order_detail_response",
            return_value=SimpleNamespace(model_dump=lambda mode=None: detail),
        ):
            first_service = self._service(db, order, shared_repository)
            first_service.update_order_status(
                order.id, _owner_scope(restaurant_id), request,
                admin_user=_admin(admin_id), idempotency_key="patch-key",
            )

            second_service = self._service(db, order, shared_repository)
            with patch.object(
                OrderDetailResponse, "model_validate", side_effect=lambda value: value
            ):
                replay = second_service.update_order_status(
                    order.id, _owner_scope(restaurant_id), request,
                    admin_user=_admin(admin_id), idempotency_key="patch-key",
                )

        self.assertEqual(len(first_service.order_repository.history), 1)
        self.assertEqual(len(second_service.order_repository.history), 0)
        self.assertEqual(replay, detail)

    def test_same_key_from_another_admin_is_a_separate_operation(self):
        # Dois lojistas do mesmo restaurante reutilizando a MESMA chave em
        # operacoes diferentes (aceitar e depois preparar). O escopo inclui o
        # id do admin, entao a chave de um nao devolve a resposta do outro.
        db = FakeDb()
        restaurant_id = uuid.uuid4()
        order = self._order(restaurant_id)
        shared_repository = FakeIdempotencyRepository()

        with patch.object(
            OrderService, "to_order_detail_response",
            return_value=SimpleNamespace(model_dump=lambda mode=None: {}),
        ):
            service_a = self._service(db, order, shared_repository)
            service_a.update_order_status(
                order.id, _owner_scope(restaurant_id), SimpleNamespace(status="accepted", note=None),
                admin_user=_admin(uuid.uuid4()), idempotency_key="mesma-chave",
            )
            service_b = self._service(db, order, shared_repository)
            service_b.update_order_status(
                order.id, _owner_scope(restaurant_id), SimpleNamespace(status="preparing", note=None),
                admin_user=_admin(uuid.uuid4()), idempotency_key="mesma-chave",
            )

        self.assertEqual(len(service_a.order_repository.history), 1)
        self.assertEqual(len(service_b.order_repository.history), 1)
        self.assertEqual(order.status, "preparing")

    def test_status_change_without_key_still_works(self):
        db = FakeDb()
        restaurant_id = uuid.uuid4()
        order = self._order(restaurant_id)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service = self._service(db, order)
            result = service.update_order_status(
                order.id, _owner_scope(restaurant_id),
                SimpleNamespace(status="accepted", note=None),
                admin_user=_admin(uuid.uuid4()),
            )

        self.assertEqual(result, "detail")
        self.assertEqual(len(service.order_repository.history), 1)
        self.assertEqual(service.idempotency_service.repository.reserve_calls, [])


if __name__ == "__main__":
    unittest.main()
