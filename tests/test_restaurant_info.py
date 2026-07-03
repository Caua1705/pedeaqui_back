import unittest
import uuid
from datetime import time
from types import SimpleNamespace

from fastapi import HTTPException

from main import app
from src.services.restaurant_service import RestaurantService


class FakeRestaurantRepository:
    def __init__(self, restaurant):
        self.restaurant = restaurant

    def get_active_by_slug(self, slug):
        return self.restaurant if slug == self.restaurant.slug else None


class FakeBranchRepository:
    def __init__(self, branches, hours=None, methods=None):
        self.branches = branches
        self.hours = hours or []
        self.methods = methods or []
        self.explicit_lookup = None

    def list_active_by_restaurant(self, restaurant_id):
        return self.branches

    def get_active_by_id_and_restaurant(self, branch_id, restaurant_id):
        self.explicit_lookup = (branch_id, restaurant_id)
        return next(
            (
                branch
                for branch in self.branches
                if branch.id == branch_id and branch.restaurant_id == restaurant_id
            ),
            None,
        )

    def list_business_hours(self, branch_id):
        return self.hours

    def list_enabled_payment_methods(self, branch_id):
        return self.methods


def make_branch(restaurant_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        restaurant_id=restaurant_id,
        name="LJ. Matriz",
        display_name="Manoel Dias Branco",
        email=None,
        phone="(85) 3025-3303",
        whatsapp="(85) 9 9754-6465",
        address_street="Av. Manoel Dias Branco",
        address_number="100",
        address_neighborhood="Praia do Futuro",
        address_city="Fortaleza",
        address_state="CE",
        address_zipcode=None,
        address="Endereço legado",
        neighborhood="Bairro legado",
        city="Fortaleza",
        state="CE",
        zipcode=None,
    )


class RestaurantInfoServiceTests(unittest.TestCase):
    def setUp(self):
        self.restaurant = SimpleNamespace(
            id=uuid.uuid4(),
            slug="restaurante-teste",
            name="Restaurante Teste",
            logo_path=None,
            is_active=True,
        )
        self.branch = make_branch(self.restaurant.id)

    def make_service(self, hours=None, methods=None, branches=None):
        service = RestaurantService(SimpleNamespace())
        service.restaurant_repository = FakeRestaurantRepository(self.restaurant)
        service.branch_repository = FakeBranchRepository(
            [self.branch] if branches is None else branches,
            hours,
            methods,
        )
        return service

    def test_builds_public_info_with_multiple_periods_and_payment_flows(self):
        hours = [
            SimpleNamespace(
                weekday=0,
                opens_at=time(11, 0),
                closes_at=time(14, 0),
                is_closed=False,
            ),
            SimpleNamespace(
                weekday=0,
                opens_at=time(17, 30),
                closes_at=time(22, 15),
                is_closed=False,
            ),
            SimpleNamespace(
                weekday=1, opens_at=None, closes_at=None, is_closed=True
            ),
        ]
        methods = [
            SimpleNamespace(
                id=uuid.uuid4(), payment_flow="online", method_type="pix",
                brand=None, label="PIX", icon_key="pix", enabled=True,
                requires_gateway=True,
            ),
            SimpleNamespace(
                id=uuid.uuid4(), payment_flow="delivery", method_type="cash",
                brand=None, label="Dinheiro", icon_key="cash", enabled=True,
                requires_gateway=False,
            ),
        ]
        service = self.make_service(hours, methods)

        result = service.get_detailed_public_info(self.restaurant.slug)

        self.assertEqual(result.branch.id, self.branch.id)
        self.assertEqual(result.branch.address.city, "Fortaleza")
        self.assertIn("Av. Manoel Dias Branco, 100", result.branch.address.full_address)
        self.assertEqual(
            [period.opens_at for period in result.business_hours[0].periods],
            ["11:00", "17:30"],
        )
        self.assertTrue(result.business_hours[1].is_closed)
        self.assertEqual(result.payment_methods.online[0].label, "PIX")
        self.assertEqual(result.payment_methods.delivery[0].label, "Dinheiro")
        self.assertEqual(result.current_day_label, service.DAY_LABELS[result.current_weekday])

    def test_explicit_branch_must_belong_to_restaurant(self):
        service = self.make_service()

        with self.assertRaises(HTTPException) as raised:
            service.get_detailed_public_info(self.restaurant.slug, uuid.uuid4())

        self.assertEqual(raised.exception.status_code, 404)

    def test_returns_404_when_restaurant_has_no_active_branch(self):
        service = self.make_service(branches=[])

        with self.assertRaises(HTTPException) as raised:
            service.get_detailed_public_info(self.restaurant.slug)

        self.assertEqual(raised.exception.status_code, 404)


class RestaurantInfoOpenAPIContractTests(unittest.TestCase):
    def test_documents_public_route_and_optional_branch_id(self):
        operation = app.openapi()["paths"]["/restaurants/{restaurant_slug}/info"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

        self.assertNotIn("security", operation)
        self.assertIn("branch_id", parameters)
        self.assertFalse(parameters["branch_id"]["required"])


if __name__ == "__main__":
    unittest.main()
