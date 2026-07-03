import unittest
import uuid
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError

from src.schemas.customer_schema import (
    CreateCustomerAddressRequest,
    ImportCustomerAddressRequest,
    ImportCustomerAddressesRequest,
    UpdateCustomerAddressRequest,
)
from src.services.customer_service import CustomerService


class FakeSession:
    def __init__(self) -> None:
        self.repository = None

    def add(self, value) -> None:
        pass

    def delete(self, value) -> None:
        self.repository.addresses.remove(value)

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def refresh(self, value) -> None:
        pass


class FakeCustomerRepository:
    def __init__(self, addresses=None) -> None:
        self.addresses = list(addresses or [])

    def lock_customer(self, customer_id):
        return SimpleNamespace(id=customer_id)

    def list_addresses(self, customer_id):
        return [
            address
            for address in self.addresses
            if address.customer_id == customer_id
        ]

    def get_address(self, customer_id, address_id):
        return next(
            (
                address
                for address in self.addresses
                if address.id == address_id and address.customer_id == customer_id
            ),
            None,
        )

    def unset_default_addresses(self, customer_id):
        for address in self.list_addresses(customer_id):
            address.is_default = False

    def create_address(self, **values):
        address = make_address(**values)
        self.addresses.append(address)
        return address


def make_address(**values):
    defaults = {
        "id": uuid.uuid4(),
        "customer_id": uuid.uuid4(),
        "client_reference": None,
        "label": None,
        "street": "Rua A",
        "number": "10",
        "neighborhood": "Centro",
        "complement": None,
        "reference": None,
        "city": "Fortaleza",
        "state": "CE",
        "zipcode": None,
        "latitude": None,
        "longitude": None,
        "is_default": False,
        "created_at": None,
        "updated_at": None,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


class CustomerAddressServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.customer = SimpleNamespace(id=uuid.uuid4())
        self.db = FakeSession()
        self.repository = FakeCustomerRepository()
        self.db.repository = self.repository
        self.service = CustomerService(self.db)
        self.service.customer_repository = self.repository

    @staticmethod
    def payload(**values):
        defaults = {
            "street": "Rua A",
            "number": "10",
            "neighborhood": "Centro",
        }
        defaults.update(values)
        return CreateCustomerAddressRequest(**defaults)

    def test_first_address_becomes_default(self):
        address = self.service.create_address(self.customer, self.payload())
        self.assertTrue(address.is_default)

    def test_second_address_does_not_change_default(self):
        first = self.service.create_address(self.customer, self.payload())
        second = self.service.create_address(
            self.customer,
            self.payload(street="Rua B"),
        )
        self.assertTrue(first.is_default)
        self.assertFalse(second.is_default)

    def test_explicit_default_unsets_previous_address(self):
        first = self.service.create_address(self.customer, self.payload())
        second = self.service.create_address(
            self.customer,
            self.payload(street="Rua B", is_default=True),
        )
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)

    def test_deleting_default_promotes_another_address(self):
        first = self.service.create_address(self.customer, self.payload())
        second = self.service.create_address(
            self.customer,
            self.payload(street="Rua B"),
        )
        self.service.delete_address(self.customer, first.id)
        self.assertTrue(second.is_default)

    def test_import_is_idempotent_by_client_reference(self):
        payload = ImportCustomerAddressesRequest(
            addresses=[
                ImportCustomerAddressRequest(
                    client_reference="local-1",
                    street="Travessa Joao Felipe",
                    number="111",
                    neighborhood="Mousa Brasil",
                    city="Fortaleza",
                    state="CE",
                )
            ]
        )
        first_result = self.service.import_addresses(self.customer, payload)
        second_result = self.service.import_addresses(self.customer, payload)
        self.assertEqual(len(first_result.created), 1)
        self.assertEqual(len(second_result.created), 0)
        self.assertEqual(len(second_result.existing), 1)
        self.assertEqual(len(self.repository.addresses), 1)

    def test_import_is_idempotent_by_normalized_fingerprint(self):
        first_payload = ImportCustomerAddressesRequest(
            addresses=[
                ImportCustomerAddressRequest(
                    street="Travessa Joao Felipe",
                    number="111",
                    neighborhood="Mousa Brasil",
                    city="Fortaleza",
                    state="CE",
                    zipcode="60123456",
                )
            ]
        )
        equivalent_payload = ImportCustomerAddressesRequest(
            addresses=[
                ImportCustomerAddressRequest(
                    street="  TRAVESSA   JOAO FELIPE ",
                    number="111",
                    neighborhood="mousa brasil",
                    city="FORTALEZA",
                    state="ce",
                    zipcode="60123-456",
                )
            ]
        )
        self.service.import_addresses(self.customer, first_payload)
        result = self.service.import_addresses(self.customer, equivalent_payload)
        self.assertEqual(len(result.created), 0)
        self.assertEqual(len(result.existing), 1)
        self.assertEqual(len(self.repository.addresses), 1)

    def test_customer_id_is_rejected_from_address_payload(self):
        with self.assertRaises(ValidationError):
            CreateCustomerAddressRequest(
                customer_id=str(self.customer.id),
                street="Rua A",
                number="10",
                neighborhood="Centro",
            )
    def test_customer_cannot_update_another_customers_address(self):
        foreign_address = make_address(customer_id=uuid.uuid4())
        self.repository.addresses.append(foreign_address)
        with self.assertRaises(HTTPException) as raised:
            self.service.update_address(
                self.customer,
                foreign_address.id,
                UpdateCustomerAddressRequest(label="Trabalho"),
            )
        self.assertEqual(raised.exception.status_code, 404)

    def test_invalid_coordinates_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.payload(latitude=91)
        with self.assertRaises(ValidationError):
            self.payload(longitude=-181)


if __name__ == "__main__":
    unittest.main()
