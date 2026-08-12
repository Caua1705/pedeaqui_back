"""Reaproveitamento da estimativa de entrega (Fase 2, bloco F).

O problema era custo: o cliente chamava POST /delivery/estimate no checkout
(geocode + rota, as duas pagas no Google) e, minutos depois, POST /orders
refazia as MESMAS duas chamadas. Dobro do custo por pedido, e a conexao de
banco presa durante o I/O externo.

A regra que estes testes travam: o cliente devolve so um token; taxa,
distancia e prazo saem do banco. Qualquer divergencia derruba o
reaproveitamento e o Google e chamado de novo — nunca o contrario.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from src.repositories.delivery_estimate_repository import DeliveryEstimateRepository
from src.schemas.delivery_schema import DeliveryAddressInput, DeliveryEstimateRequest
from src.schemas.order_schema import AddressInput, CreateOrderRequest
from src.services.delivery_estimate_service import (
    DeliveryEstimateService,
    build_address_fingerprint,
)
from src.services.order_service import OrderService


RESTAURANT_ID = uuid.uuid4()
BRANCH_ID = uuid.uuid4()


def address_input(**overrides):
    values = {
        "street": "Travessa Joao Felipe",
        "number": "111",
        "neighborhood": "Mousa Brasil",
        "city": "Fortaleza",
        "state": "CE",
        "zipcode": "60000-000",
        "latitude": Decimal("-3.7500000"),
        "longitude": Decimal("-38.5500000"),
    }
    values.update(overrides)
    return AddressInput(**values)


def estimate_request(address=None, address_id=None):
    return DeliveryEstimateRequest(
        branch_id=BRANCH_ID,
        address_id=address_id,
        address=address if address_id is None else None,
    )


def delivery_address(**overrides):
    values = {
        "street": "Travessa Joao Felipe",
        "number": "111",
        "neighborhood": "Mousa Brasil",
        "city": "Fortaleza",
        "state": "CE",
        "zipcode": "60000-000",
        "latitude": Decimal("-3.7500000"),
        "longitude": Decimal("-38.5500000"),
    }
    values.update(overrides)
    return DeliveryAddressInput(**values)


def stored_estimate(fingerprint, **overrides):
    values = {
        "id": uuid.uuid4(),
        "token": "token-da-estimativa",
        "restaurant_id": RESTAURANT_ID,
        "branch_id": BRANCH_ID,
        "customer_id": None,
        "address_fingerprint": fingerprint,
        "distance_km": Decimal("4.20"),
        "travel_time_min": 18,
        "prep_time_min": 40,
        "prep_time_max": 60,
        "eta_min": 58,
        "eta_max": 78,
        "delivery_fee": Decimal("11.30"),
        "latitude": Decimal("-3.7500000"),
        "longitude": Decimal("-38.5500000"),
        "provider": "google_routes",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeEstimateRepository:
    def __init__(self, estimate=None):
        self.estimate = estimate
        self.queried_token = None

    def get_valid_by_token(self, token, now):
        self.queried_token = token
        if self.estimate is None or self.estimate.token != token:
            return None
        # A expiracao e filtrada no SQL; o fake reproduz a regra.
        if self.estimate.expires_at <= now:
            return None
        return self.estimate


def build_service(stored=None):
    service = OrderService(SimpleNamespace())
    service.delivery_estimate_repository = FakeEstimateRepository(stored)
    return service


def order_payload(token="token-da-estimativa", address=None):
    return CreateOrderRequest.model_validate({
        "branch_id": str(BRANCH_ID),
        "order_type": "delivery",
        "payment_method": "cash",
        "delivery_estimate_token": token,
        "customer": {"name": "Ana", "phone": "85999999999"},
        "address": (address or address_input()).model_dump(mode="json"),
        "items": [{"product_id": str(uuid.uuid4()), "quantity": 1}],
    })


class FingerprintTests(unittest.TestCase):
    def test_same_address_produces_the_same_fingerprint(self):
        self.assertEqual(
            build_address_fingerprint(estimate_request(delivery_address())),
            build_address_fingerprint(estimate_request(delivery_address())),
        )

    def test_another_street_produces_another_fingerprint(self):
        self.assertNotEqual(
            build_address_fingerprint(estimate_request(delivery_address())),
            build_address_fingerprint(estimate_request(delivery_address(street="Rua Bem Longe"))),
        )

    def test_another_number_produces_another_fingerprint(self):
        self.assertNotEqual(
            build_address_fingerprint(estimate_request(delivery_address())),
            build_address_fingerprint(estimate_request(delivery_address(number="999"))),
        )

    def test_coordinates_far_apart_produce_another_fingerprint(self):
        # O caso que importa: estimar o endereco perto e fechar o pedido no
        # distante pagando a taxa do primeiro.
        self.assertNotEqual(
            build_address_fingerprint(estimate_request(delivery_address())),
            build_address_fingerprint(
                estimate_request(delivery_address(latitude=Decimal("-3.9000000")))
            ),
        )

    def test_zipcode_formatting_does_not_change_the_fingerprint(self):
        self.assertEqual(
            build_address_fingerprint(estimate_request(delivery_address(zipcode="60000-000"))),
            build_address_fingerprint(estimate_request(delivery_address(zipcode="60000000"))),
        )

    def test_case_and_spacing_do_not_change_the_fingerprint(self):
        self.assertEqual(
            build_address_fingerprint(estimate_request(delivery_address())),
            build_address_fingerprint(
                estimate_request(delivery_address(street="  TRAVESSA JOAO FELIPE "))
            ),
        )

    def test_saved_address_is_identified_by_its_id(self):
        address_id = uuid.uuid4()
        self.assertEqual(
            build_address_fingerprint(estimate_request(address_id=address_id)),
            f"address_id:{address_id}",
        )


class ReuseTests(unittest.TestCase):
    def _reuse(self, service, payload, current_customer=None):
        address = payload.address
        return service._reuse_stored_estimate(
            token=payload.delivery_estimate_token,
            restaurant_id=RESTAURANT_ID,
            branch_id=BRANCH_ID,
            estimate_request=OrderService._build_estimate_request(payload, address),
            current_customer=current_customer,
        )

    def _fingerprint_of(self, payload):
        return build_address_fingerprint(
            OrderService._build_estimate_request(payload, payload.address)
        )

    def test_valid_token_reuses_the_stored_values(self):
        payload = order_payload()
        service = build_service(stored_estimate(self._fingerprint_of(payload)))

        reused = self._reuse(service, payload)

        self.assertIsNotNone(reused)
        self.assertTrue(reused.serviceable)
        self.assertEqual(reused.delivery_fee, 11.3)
        self.assertEqual(reused.distance_km, 4.2)
        self.assertEqual(reused.eta_min, 58)
        self.assertEqual(reused.prep_time_max, 60)

    def test_without_token_nothing_is_reused(self):
        payload = order_payload(token=None)
        service = build_service(stored_estimate(self._fingerprint_of(payload)))

        self.assertIsNone(self._reuse(service, payload))
        self.assertIsNone(service.delivery_estimate_repository.queried_token)

    def test_expired_estimate_is_not_reused(self):
        payload = order_payload()
        expired = stored_estimate(
            self._fingerprint_of(payload),
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        service = build_service(expired)

        self.assertIsNone(self._reuse(service, payload))

    def test_estimate_of_another_address_is_not_reused(self):
        # A protecao central: o token nao carrega o valor, e o valor
        # guardado so vale para o endereco que foi medido.
        payload = order_payload()
        service = build_service(stored_estimate("address:outro-endereco-qualquer"))

        self.assertIsNone(self._reuse(service, payload))

    def test_estimate_of_another_branch_is_not_reused(self):
        payload = order_payload()
        service = build_service(
            stored_estimate(self._fingerprint_of(payload), branch_id=uuid.uuid4())
        )

        self.assertIsNone(self._reuse(service, payload))

    def test_estimate_of_another_restaurant_is_not_reused(self):
        payload = order_payload()
        service = build_service(
            stored_estimate(self._fingerprint_of(payload), restaurant_id=uuid.uuid4())
        )

        self.assertIsNone(self._reuse(service, payload))

    def test_estimate_of_another_customer_is_not_reused(self):
        payload = order_payload()
        service = build_service(
            stored_estimate(self._fingerprint_of(payload), customer_id=uuid.uuid4())
        )

        self.assertIsNone(self._reuse(service, payload, current_customer=SimpleNamespace(id=uuid.uuid4())))

    def test_guest_cannot_use_the_estimate_of_a_logged_customer(self):
        payload = order_payload()
        service = build_service(
            stored_estimate(self._fingerprint_of(payload), customer_id=uuid.uuid4())
        )

        self.assertIsNone(self._reuse(service, payload, current_customer=None))

    def test_unknown_token_is_not_reused(self):
        payload = order_payload(token="chute")
        service = build_service(stored_estimate(self._fingerprint_of(payload)))

        self.assertIsNone(self._reuse(service, payload))


class GoogleIsNotCalledTests(unittest.TestCase):
    """O objetivo do bloco F: a chamada paga nao acontece duas vezes."""

    def test_order_with_a_valid_token_does_not_call_the_estimate_service(self):
        payload = order_payload()
        fingerprint = build_address_fingerprint(
            OrderService._build_estimate_request(payload, payload.address)
        )
        service = build_service(stored_estimate(fingerprint))

        with patch.object(DeliveryEstimateService, "estimate", side_effect=_no_google):
            estimate = service._estimate_delivery(
                "junior",
                payload,
                payload.address,
                None,
                restaurant_id=RESTAURANT_ID,
            )

        self.assertEqual(estimate.delivery_fee, 11.3)

    def test_order_without_a_token_falls_back_to_calling_the_estimate_service(self):
        payload = order_payload(token=None)
        service = build_service()
        called = []

        def record_call(self, slug, request, customer):
            called.append(slug)
            return SimpleNamespace(serviceable=True, delivery_fee=9.9, message=None, reason=None)

        with patch.object(DeliveryEstimateService, "estimate", record_call):
            service._estimate_delivery(
                "junior",
                payload,
                payload.address,
                None,
                restaurant_id=RESTAURANT_ID,
            )

        self.assertEqual(called, ["junior"])


class TokenComparisonTests(unittest.TestCase):
    """A reconferencia em tempo constante do repositorio de estimativa.

    Ela e inalcancavel pelo caminho normal: se o SELECT devolveu a linha, o
    `=` do Postgres ja disse que os tokens sao iguais. O teste chega nela
    trocando o que a consulta devolve — que e exatamente o cenario que a
    linha existe para cobrir: o dia em que o WHERE deixar de ser igualdade
    exata (um `ilike`, uma collation que aproxime formas Unicode).
    """

    def _repository(self, returned_estimate):
        repository = DeliveryEstimateRepository(SimpleNamespace())
        repository.db = SimpleNamespace(scalar=lambda stmt: returned_estimate)
        return repository

    def test_a_row_whose_token_does_not_match_is_discarded(self):
        outra = SimpleNamespace(token="token-de-outra-estimativa")
        repository = self._repository(outra)

        found = repository.get_valid_by_token("token-pedido", datetime.now(timezone.utc))

        self.assertIsNone(found)

    def test_the_matching_row_comes_back(self):
        minha = SimpleNamespace(token="token-pedido")
        repository = self._repository(minha)

        found = repository.get_valid_by_token("token-pedido", datetime.now(timezone.utc))

        self.assertIs(found, minha)

    def test_no_row_is_none(self):
        repository = self._repository(None)

        self.assertIsNone(
            repository.get_valid_by_token("token-pedido", datetime.now(timezone.utc))
        )


def _no_google(*args, **kwargs):
    raise AssertionError("Google nao deveria ser chamado com estimativa reaproveitada")


if __name__ == "__main__":
    unittest.main()
