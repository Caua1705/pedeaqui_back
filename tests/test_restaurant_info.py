import unittest
import uuid
from datetime import time
from types import SimpleNamespace

from fastapi import HTTPException

from main import app
from src.models.branch_model import Branch
from src.services.restaurant_service import RestaurantService
from tests import fabricas


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

    def get_default_branch(self, restaurant_id):
        """Espelha o repositorio de verdade: a primeira da listagem, que ja
        vem ordenada por `is_main DESC`. Ver o docstring de
        `BranchRepository.get_default_branch` para as duas regras que
        existiam antes de esta existir."""
        branches = self.list_active_by_restaurant(restaurant_id)
        return branches[0] if branches else None

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


def make_branch(restaurant_id, **sobrescreve):
    """Filial transiente — `Branch` de verdade, sem sessão e sem banco.

    Model real e não `SimpleNamespace` porque é justamente aqui que a
    diferença aparece: `_build_address` lê seis atributos, e um dublê de
    atributos livres responderia qualquer nome que o teste escrevesse — o
    teste ficaria verde descrevendo uma filial que a aplicação não produz.
    Com o `Branch`, coluna que não passe vale `None` e nome errado é
    `TypeError` na hora.
    """
    padrao = dict(
        id=uuid.uuid4(),
        restaurant_id=restaurant_id,
        name="LJ. Matriz",
        slug="matriz",
        display_name="Manoel Dias Branco",
        email=None,
        phone="(85) 3025-3303",
        whatsapp="(85) 9 9754-6465",
        # O conjunto VIVO: é o que `AdminBranchUpdate` grava, e o único que
        # `_build_address` lê.
        address="Av. Manoel Dias Branco",
        neighborhood="Praia do Futuro",
        city="Fortaleza",
        state="CE",
        zipcode=None,
        # `address_number` é a única das seis `address_*` que sobreviveu: não
        # existe campo de número no painel, então ela não sobrescreve ninguém.
        address_number="100",
    )
    return Branch(**{**padrao, **sobrescreve})


class RestaurantInfoServiceTests(unittest.TestCase):
    def setUp(self):
        self.restaurant = fabricas.restaurante(
            slug="restaurante-teste", name="Restaurante Teste"
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
            fabricas.horario(opens_at=time(11, 0), closes_at=time(14, 0)),
            fabricas.horario(opens_at=time(17, 30), closes_at=time(22, 15)),
            fabricas.horario(weekday=1, opens_at=None, closes_at=None, is_closed=True),
        ]
        methods = [
            fabricas.forma_de_pagamento(
                payment_flow="online", method_type="pix", label="PIX",
                icon_key="pix", requires_gateway=True,
            ),
            fabricas.forma_de_pagamento(
                payment_flow="delivery", method_type="cash", label="Dinheiro",
                icon_key="cash",
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


class BranchAddressTests(unittest.TestCase):
    """O endereço público sai do conjunto VIVO, o que o painel grava.

    As seis `branches.address_*` são resto do schema pré-Alembic e nada as
    escreve — mas `_build_address` as LIA primeiro, e elas venciam. Numa
    filial com elas preenchidas, o lojista corrigia o endereço no painel e o
    app do cliente continuava mostrando o antigo, sem erro e sem log.
    """

    def setUp(self):
        self.restaurant_id = uuid.uuid4()

    def test_o_conjunto_morto_nao_sobrescreve_mais_o_que_o_painel_gravou(self):
        filial = make_branch(
            self.restaurant_id,
            address="Av. Manoel Dias Branco",
            neighborhood="Praia do Futuro",
            city="Fortaleza",
            state="CE",
            zipcode="60182-015",
            # O que o painel não alcança, e que antes vencia.
            address_street="Rua Antiga",
            address_neighborhood="Bairro Antigo",
            address_city="Cidade Antiga",
            address_state="XX",
            address_zipcode="00000-000",
        )

        endereco = RestaurantService._build_address(filial)

        self.assertEqual(endereco.street, "Av. Manoel Dias Branco")
        self.assertEqual(endereco.neighborhood, "Praia do Futuro")
        self.assertEqual(endereco.city, "Fortaleza")
        self.assertEqual(endereco.state, "CE")
        self.assertEqual(endereco.zipcode, "60182-015")
        self.assertNotIn("Antig", endereco.full_address)

    def test_o_numero_continua_saindo_de_address_number(self):
        """A única das seis que sobrou, porque não tem par vivo.

        Não existe `branches.number` nem campo de número em
        `AdminBranchUpdate`: `address_number` não sobrescreve ninguém, e
        largá-la apagaria o número da casa do endereço público.
        """
        filial = make_branch(self.restaurant_id, address_number="100")

        endereco = RestaurantService._build_address(filial)

        self.assertEqual(endereco.number, "100")
        self.assertIn("Av. Manoel Dias Branco, 100", endereco.full_address)

    def test_filial_sem_numero_monta_o_endereco_do_mesmo_jeito(self):
        """Coluna não passada vale `None` no model transiente — e é o caso real:
        nada no código escreve `address_number`, então a filial nova nasce sem.
        """
        filial = make_branch(self.restaurant_id, address_number=None, zipcode=None)

        endereco = RestaurantService._build_address(filial)

        self.assertIsNone(endereco.number)
        self.assertEqual(
            endereco.full_address,
            "Av. Manoel Dias Branco - Praia do Futuro - Fortaleza - CE",
        )


class RestaurantInfoOpenAPIContractTests(unittest.TestCase):
    def test_documents_public_route_and_optional_branch_id(self):
        operation = app.openapi()["paths"]["/restaurants/{restaurant_slug}/info"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

        self.assertNotIn("security", operation)
        self.assertIn("branch_id", parameters)
        self.assertFalse(parameters["branch_id"]["required"])


if __name__ == "__main__":
    unittest.main()
