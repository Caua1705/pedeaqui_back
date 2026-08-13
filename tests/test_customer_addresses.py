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
        self.unset_calls = 0

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
        # Contado para o teste do import repetido: promover um endereco que
        # JA e o padrao seria um UPDATE inutil em toda sincronizacao da app.
        self.unset_calls += 1
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

    # -- as tres ramificacoes do import que nao tinham rede -----------------
    #
    # CARACTERIZACAO: descrevem o que o codigo faz HOJE, antes de ele ser
    # simplificado. Sao elas que dizem se a simplificacao mudou
    # comportamento — sem isto, o refactor seria fe.

    @staticmethod
    def endereco_importado(**values):
        defaults = {
            "street": "Travessa Joao Felipe",
            "number": "111",
            "neighborhood": "Mousa Brasil",
            "city": "Fortaleza",
            "state": "CE",
        }
        defaults.update(values)
        return ImportCustomerAddressRequest(**defaults)

    def test_the_same_address_twice_in_one_request_is_ignored_once(self):
        """`duplicate_in_request`: a app manda a lista inteira do aparelho, e
        o mesmo endereco pode aparecer duas vezes nela — salvo uma vez com
        `client_reference` e outra sem, por exemplo. A segunda ocorrencia nao
        vira endereco novo nem erro: vira uma linha em `ignored`."""
        payload = ImportCustomerAddressesRequest(
            addresses=[self.endereco_importado(), self.endereco_importado()]
        )

        result = self.service.import_addresses(self.customer, payload)

        self.assertEqual(len(result.created), 1)
        self.assertEqual(len(result.ignored), 1)
        self.assertEqual(result.ignored[0].reason, "duplicate_in_request")
        self.assertEqual(len(self.repository.addresses), 1)

    def test_the_duplicate_is_detected_by_client_reference_not_by_content(self):
        """Dois enderecos DIFERENTES com o mesmo `client_reference` — o
        aparelho reaproveitou o id local. O segundo e ignorado pela chave, sem
        olhar o conteudo."""
        payload = ImportCustomerAddressesRequest(
            addresses=[
                self.endereco_importado(client_reference="local-1"),
                self.endereco_importado(client_reference="local-1", street="Outra Rua"),
            ]
        )

        result = self.service.import_addresses(self.customer, payload)

        self.assertEqual(len(result.created), 1)
        self.assertEqual(result.ignored[0].client_reference, "local-1")

    def test_importing_an_existing_address_as_default_promotes_it(self):
        """O `matched` que vira padrao: o endereco JA existe e chega marcado
        como padrao no import. Ele nao e recriado — o que existe e promovido,
        e o padrao anterior perde o posto."""
        antigo = self.service.create_address(self.customer, self.payload())
        self.assertTrue(antigo.is_default)

        self.service.import_addresses(
            self.customer,
            ImportCustomerAddressesRequest(addresses=[self.endereco_importado()]),
        )
        importado = self.repository.addresses[-1]
        self.assertFalse(importado.is_default)

        result = self.service.import_addresses(
            self.customer,
            ImportCustomerAddressesRequest(
                addresses=[self.endereco_importado(is_default=True)]
            ),
        )

        self.assertEqual(len(result.created), 0)
        self.assertEqual(len(result.existing), 1)
        self.assertTrue(importado.is_default)
        self.assertFalse(antigo.is_default)

    def test_an_existing_address_already_default_is_not_touched(self):
        """O outro lado da mesma ramificacao: `is_default and not
        matched.is_default`. Se ele JA e o padrao, nada acontece — e o que
        evita um `unset_default_addresses` inutil a cada import repetido."""
        self.service.import_addresses(
            self.customer,
            ImportCustomerAddressesRequest(
                addresses=[self.endereco_importado(is_default=True)]
            ),
        )
        importado = self.repository.addresses[0]
        chamadas_antes = self.repository.unset_calls

        self.service.import_addresses(
            self.customer,
            ImportCustomerAddressesRequest(
                addresses=[self.endereco_importado(is_default=True)]
            ),
        )

        self.assertTrue(importado.is_default)
        self.assertEqual(self.repository.unset_calls, chamadas_antes)

    def test_when_nobody_is_default_the_first_address_is_promoted(self):
        """O fallback de "ninguem e padrao".

        O cliente nunca fica sem endereco padrao: se ao fim do import nenhum
        estiver marcado, o primeiro assume. Sem isso, o checkout abriria sem
        endereco selecionado para quem apagou o padrao antes de importar.
        """
        primeiro = self.service.create_address(self.customer, self.payload())
        primeiro.is_default = False

        self.service.import_addresses(
            self.customer,
            ImportCustomerAddressesRequest(addresses=[self.endereco_importado()]),
        )

        self.assertTrue(primeiro.is_default)
        self.assertEqual(
            sum(address.is_default for address in self.repository.addresses), 1
        )

    def test_an_import_into_an_empty_account_makes_the_first_one_default(self):
        """`is_default = imported.is_default or not saved_addresses`: numa
        conta sem endereco nenhum, o primeiro do import vira padrao mesmo sem
        pedir."""
        result = self.service.import_addresses(
            self.customer,
            ImportCustomerAddressesRequest(
                addresses=[
                    self.endereco_importado(),
                    self.endereco_importado(street="Rua Segunda"),
                ]
            ),
        )

        self.assertTrue(result.created[0].is_default)
        self.assertFalse(result.created[1].is_default)

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
