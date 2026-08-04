import json
import unittest
import uuid

from pydantic import ValidationError

from main import app
from src.schemas.customer_schema import ImportCustomerAddressesRequest
from src.schemas.delivery_schema import DeliveryEstimateRequest
from src.schemas.payment_schema import PaymentErrorCode


PAYMENT_ROUTE = "/restaurants/{restaurant_slug}/orders/{tracking_token}/payment"


class PaymentErrorContractTests(unittest.TestCase):
    """O erro da cobranca tem que estar PUBLICADO, nao so implementado.

    O frontend escreve o parser a partir do /openapi.json. Um formato que
    existe no codigo mas nao no documento e um formato que ninguem consegue
    consumir sem ler o backend.
    """

    @classmethod
    def setUpClass(cls):
        cls.schema = app.openapi()
        cls.operation = cls.schema["paths"][PAYMENT_ROUTE]["post"]

    def test_the_error_statuses_are_declared(self):
        self.assertIn("502", self.operation["responses"])
        self.assertIn("503", self.operation["responses"])

    def test_the_declared_body_carries_the_detail_envelope(self):
        # HTTPException entrega {"detail": {...}}. Anunciar o detail na raiz
        # faria o frontend escrever o parser contra um formato que a rota
        # nunca devolve.
        for status_code in ("502", "503"):
            ref = self.operation["responses"][status_code]["content"]["application/json"]["schema"]["$ref"]
            self.assertEqual(ref, "#/components/schemas/PaymentErrorResponse")

        envelope = self.schema["components"]["schemas"]["PaymentErrorResponse"]
        self.assertEqual(
            envelope["properties"]["detail"]["$ref"],
            "#/components/schemas/PaymentErrorDetail",
        )

    def test_the_detail_publishes_every_field_the_frontend_reads(self):
        detail = self.schema["components"]["schemas"]["PaymentErrorDetail"]

        for field in ("code", "message", "retryable", "provider_error_code"):
            self.assertIn(field, detail["properties"])
        # provider_error_code e o unico opcional: nem todo erro tem
        # referencia do provedor.
        self.assertEqual(sorted(detail["required"]), ["code", "message", "retryable"])

    def test_the_possible_codes_are_published_as_an_enum(self):
        # Sem a lista, o frontend so consegue tratar `retryable` e cai no
        # texto generico para os dois casos definitivos, que pedem coisas
        # diferentes do cliente.
        published = self.schema["components"]["schemas"]["PaymentErrorCode"]["enum"]

        self.assertEqual(sorted(published), sorted(code.value for code in PaymentErrorCode))
        self.assertIn("gateway_unavailable", published)
        self.assertIn("payment_unavailable", published)
        self.assertIn("payment_rejected", published)

    def test_the_enum_is_reachable_from_the_route(self):
        # Um enum solto em components que nenhuma rota referencia nao chega
        # a gerador de cliente nenhum.
        detail = self.schema["components"]["schemas"]["PaymentErrorDetail"]

        self.assertIn("PaymentErrorCode", json.dumps(detail["properties"]["code"]))


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
        for field in ("customer_id", "delivery_fee", "distance_km", "eta_min", "prep_time_max"):
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

    def test_openapi_documents_cashback_transactions_pagination_and_auth(self):
        operation = app.openapi()["paths"]["/customers/me/cashback/transactions"]["get"]
        parameters = {item["name"]: item for item in operation["parameters"]}

        self.assertEqual(operation["security"], [{"HTTPBearer": []}])
        self.assertEqual(parameters["limit"]["schema"]["default"], 20)
        self.assertEqual(parameters["limit"]["schema"]["maximum"], 50)
        self.assertEqual(parameters["offset"]["schema"]["default"], 0)


if __name__ == "__main__":
    unittest.main()
