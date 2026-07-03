import unittest
import uuid

from pydantic import ValidationError

from main import app
from src.schemas.customer_schema import ImportCustomerAddressesRequest
from src.schemas.delivery_schema import DeliveryEstimateRequest


class DeliveryContractTests(unittest.TestCase):
    def test_accepts_address_id_and_empty_branch_id(self):
        address_id = uuid.uuid4()
        payload = DeliveryEstimateRequest.model_validate(
            {"branch_id": "", "address_id": str(address_id)}
        )
        self.assertIsNone(payload.branch_id)
        self.assertEqual(payload.address_id, address_id)

    def test_normalizes_inline_address_legacy_values(self):
        payload = DeliveryEstimateRequest.model_validate(
            {
                "address": {
                    "street": " Travessa Joao Felipe ",
                    "number": " 111 ",
                    "neighborhood": " Mousa Brasil ",
                    "city": "",
                    "state": None,
                    "zip_code": " 60123-456 ",
                    "latitude": "",
                    "longitude": "",
                }
            }
        )
        self.assertEqual(payload.address.city, "Fortaleza")
        self.assertEqual(payload.address.state, "CE")
        self.assertEqual(payload.address.zipcode, "60123-456")
        self.assertIsNone(payload.address.latitude)
        self.assertIsNone(payload.address.longitude)

    def test_requires_exactly_one_address_source_with_clear_message(self):
        for raw in ({}, {"address_id": str(uuid.uuid4()), "address": {
            "street": "Rua A", "number": "1", "neighborhood": "Centro"
        }}):
            with self.assertRaises(ValidationError) as raised:
                DeliveryEstimateRequest.model_validate(raw)
            self.assertIn(
                "Informe exatamente um entre address_id e address",
                str(raised.exception),
            )

    def test_rejects_customer_id_and_untrusted_calculation_fields(self):
        for field in ("customer_id", "delivery_fee", "distance_km", "eta_min"):
            with self.assertRaises(ValidationError):
                DeliveryEstimateRequest.model_validate(
                    {field: "1", "address_id": str(uuid.uuid4())}
                )


class AddressImportContractTests(unittest.TestCase):
    def test_accepts_empty_list(self):
        payload = ImportCustomerAddressesRequest.model_validate({"addresses": []})
        self.assertEqual(payload.addresses, [])

    def test_requires_addresses_to_be_a_list(self):
        with self.assertRaises(ValidationError) as raised:
            ImportCustomerAddressesRequest.model_validate({"addresses": {}})
        self.assertIn("addresses", str(raised.exception))

    def test_accepts_zip_alias_and_normalizes_empty_optional_fields(self):
        payload = ImportCustomerAddressesRequest.model_validate(
            {
                "addresses": [{
                    "client_reference": " local-uuid ",
                    "label": "",
                    "street": " Travessa Joao Felipe ",
                    "number": " 111 ",
                    "neighborhood": " Mousa Brasil ",
                    "city": "Fortaleza",
                    "state": "CE",
                    "zip_code": "60123-456",
                    "complement": "",
                    "reference": None,
                    "latitude": "",
                    "longitude": None,
                    "is_default": True,
                }]
            }
        )
        address = payload.addresses[0]
        self.assertEqual(address.zipcode, "60123456")
        self.assertIsNone(address.label)
        self.assertIsNone(address.complement)
        self.assertIsNone(address.latitude)

    def test_rejects_customer_id_and_more_than_twenty_addresses(self):
        address = {"street": "Rua A", "number": "1", "neighborhood": "Centro"}
        with self.assertRaises(ValidationError):
            ImportCustomerAddressesRequest.model_validate(
                {"addresses": [{**address, "customer_id": str(uuid.uuid4())}]}
            )
        with self.assertRaises(ValidationError):
            ImportCustomerAddressesRequest.model_validate(
                {"addresses": [address] * 21}
            )

    def test_openapi_documents_both_request_bodies(self):
        paths = app.openapi()["paths"]
        self.assertIn(
            "requestBody",
            paths["/restaurants/{restaurant_slug}/delivery/estimate"]["post"],
        )
        operation = paths["/customers/me/addresses/import"]["post"]
        self.assertIn("requestBody", operation)
        self.assertEqual(operation["security"], [{"HTTPBearer": []}])


if __name__ == "__main__":
    unittest.main()
