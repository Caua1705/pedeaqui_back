import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError

from src.integrations.google_maps_routes_client import (
    GoogleMapsUnavailableError,
    RouteMetrics,
)
from src.schemas.delivery_schema import (
    DeliveryAddressInput,
    DeliveryEstimateRequest,
)
from src.services.delivery_estimate_service import DeliveryEstimateService


class FakeMapsClient:
    def __init__(self, *, unavailable=False) -> None:
        self.unavailable = unavailable
        self.calls = 0

    def compute_route(self, origin, destination):
        self.calls += 1
        if self.unavailable:
            raise GoogleMapsUnavailableError()
        return RouteMetrics(distance_km=4.2, travel_time_min=18)

    def geocode(self, address):
        raise AssertionError("Coordinates should avoid geocoding in these tests")


class FakeCache:
    def __init__(self) -> None:
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class FakeZoneRepository:
    def __init__(self, zones=None, matching=None) -> None:
        self.zones = list(zones or [])
        self.matching = matching

    def list_active_by_branch(self, restaurant_id, branch_id):
        return self.zones

    def get_active_by_neighborhood(self, **kwargs):
        return self.matching


class DeliveryEstimateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.restaurant = SimpleNamespace(id=uuid.uuid4(), slug="restaurante")
        self.branch = SimpleNamespace(
            id=uuid.uuid4(),
            latitude=Decimal("-3.7300000"),
            longitude=Decimal("-38.5200000"),
            address="Rua da Filial, 1",
            neighborhood="Centro",
            city="Fortaleza",
            state="CE",
            zipcode=None,
            is_main=True,
        )
        self.settings = SimpleNamespace(
            accepts_delivery=True,
            default_delivery_fee=Decimal("8.00"),
            estimated_delivery_time_min=60,
            estimated_delivery_time_max=75,
        )
        self.maps = FakeMapsClient()
        self.cache = FakeCache()
        self.service = DeliveryEstimateService.__new__(DeliveryEstimateService)
        self.service.restaurant_service = SimpleNamespace(
            get_active_restaurant=lambda slug: self.restaurant
        )
        self.service.branch_repository = SimpleNamespace(
            get_active_by_id_and_restaurant=lambda branch_id, restaurant_id: (
                self.branch if branch_id == self.branch.id else None
            ),
            list_active_by_restaurant=lambda restaurant_id: [self.branch],
        )
        self.service.customer_repository = SimpleNamespace(get_address=lambda *_: None)
        self.service.delivery_zone_repository = FakeZoneRepository()
        self.service.menu_repository = SimpleNamespace(
            get_settings=lambda restaurant_id: self.settings
        )
        self.service.maps_client = self.maps
        self.service.cache = self.cache

    def request(self):
        return DeliveryEstimateRequest(
            address=DeliveryAddressInput(
                street="Travessa Joao Felipe",
                number="111",
                neighborhood="Mousa Brasil",
                city="Fortaleza",
                state="CE",
                latitude=Decimal("-3.7500000"),
                longitude=Decimal("-38.5500000"),
            )
        )

    def test_google_route_builds_real_eta(self):
        result = self.service.estimate("restaurante", self.request(), None)
        self.assertTrue(result.serviceable)
        self.assertEqual(result.provider, "google_routes")
        self.assertEqual(result.distance_km, 4.2)
        self.assertEqual(result.travel_time_min, 18)
        self.assertEqual(result.eta_min, 78)
        self.assertEqual(result.eta_max, 93)
        self.assertEqual(result.delivery_fee, 8.0)

    def test_google_failure_uses_explicit_fallback(self):
        self.service.maps_client = FakeMapsClient(unavailable=True)
        result = self.service.estimate("restaurante", self.request(), None)
        self.assertTrue(result.serviceable)
        self.assertTrue(result.fallback)
        self.assertEqual(result.provider, "configured_fallback")
        self.assertIsNone(result.distance_km)

    def test_address_id_requires_authentication(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.estimate(
                "restaurante",
                DeliveryEstimateRequest(address_id=uuid.uuid4()),
                None,
            )
        self.assertEqual(raised.exception.status_code, 401)

    def test_configured_zones_reject_unknown_neighborhood(self):
        self.service.delivery_zone_repository = FakeZoneRepository(
            zones=[SimpleNamespace(id=uuid.uuid4())],
            matching=None,
        )
        result = self.service.estimate("restaurante", self.request(), None)
        self.assertFalse(result.serviceable)
        self.assertEqual(result.reason, "outside_delivery_area")
        self.assertEqual(self.maps.calls, 0)

    def test_request_requires_exactly_one_address_source(self):
        with self.assertRaises(ValidationError):
            DeliveryEstimateRequest()
        with self.assertRaises(ValidationError):
            DeliveryEstimateRequest(
                address_id=uuid.uuid4(),
                address=self.request().address,
            )

    def test_customer_id_is_rejected(self):
        with self.assertRaises(ValidationError):
            DeliveryEstimateRequest(
                customer_id=uuid.uuid4(),
                address=self.request().address,
            )


if __name__ == "__main__":
    unittest.main()
