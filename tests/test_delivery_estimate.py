import unittest
import uuid
from datetime import time
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
from src.services.branch_hours_service import BranchHoursService
from src.services.delivery_estimate_service import DeliveryEstimateService


def open_period(prep_time_min, prep_time_max, opens_at=time(0, 0), closes_at=time(23, 59)):
    """Faixa de funcionamento que cobre o dia inteiro.

    Depois da Fase 2 uma faixa sem `opens_at`/`closes_at` nao vale mais como
    "sempre aberto": ela e ignorada e a filial fica fechada. Os testes que
    antes usavam None/None passam a declarar o dia inteiro.
    """
    return SimpleNamespace(
        opens_at=opens_at,
        closes_at=closes_at,
        prep_time_min=prep_time_min,
        prep_time_max=prep_time_max,
        is_closed=False,
    )


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
            delivery_base_fee=Decimal("5.00"),
            delivery_fee_per_km=Decimal("1.50"),
            delivery_min_fee=Decimal("8.00"),
            delivery_max_fee=Decimal("20.00"),
            delivery_max_distance_km=Decimal("10.00"),
        )
        self.settings = SimpleNamespace(
            accepts_delivery=True,
            estimated_delivery_time_min=60,
            estimated_delivery_time_max=75,
        )
        self.maps = FakeMapsClient()
        self.cache = FakeCache()
        self.business_hours = []
        self.service = DeliveryEstimateService.__new__(DeliveryEstimateService)
        self.service.restaurant_service = SimpleNamespace(
            get_active_restaurant=lambda slug: self.restaurant
        )
        self.service.branch_repository = SimpleNamespace(
            get_active_by_id_and_restaurant=lambda branch_id, restaurant_id: (
                self.branch if branch_id == self.branch.id else None
            ),
            list_active_by_restaurant=lambda restaurant_id: [self.branch],
            # A filial padrao passou a sair de UM lugar (`get_default_branch`),
            # partilhado com o /restaurants/{slug}/info. O dublê espelha a
            # regra de la: a primeira da listagem ja ordenada por is_main.
            get_default_branch=lambda restaurant_id: self.branch,
            list_business_hours_by_weekday=lambda branch_id, weekday: self.business_hours,
        )
        # Servico real de horario apontando para o mesmo repositorio fake: a
        # escolha da faixa e regra de negocio e nao deve ser mockada.
        self.service.branch_hours_service = BranchHoursService.__new__(BranchHoursService)
        self.service.branch_hours_service.branch_repository = self.service.branch_repository
        self.service.customer_repository = SimpleNamespace(get_address=lambda *_: None)
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
        self.business_hours = [open_period(40, 60)]

        result = self.service.estimate("restaurante", self.request(), None)
        self.assertTrue(result.serviceable)
        self.assertEqual(result.provider, "google_routes")
        self.assertEqual(result.distance_km, 4.2)
        self.assertEqual(result.travel_time_min, 18)
        self.assertEqual(result.prep_time_min, 40)
        self.assertEqual(result.prep_time_max, 60)
        self.assertEqual(result.eta_min, 58)
        self.assertEqual(result.eta_max, 78)
        self.assertEqual(result.delivery_fee, 11.3)

    def test_delivery_fee_respects_min_and_max_limits(self):
        self.business_hours = [open_period(40, 60)]
        self.branch.delivery_min_fee = Decimal("15.00")
        result = self.service.estimate("restaurante", self.request(), None)
        self.assertTrue(result.serviceable)
        self.assertEqual(result.delivery_fee, 15.0)

        self.cache = FakeCache()
        self.service.cache = self.cache
        self.branch.delivery_min_fee = Decimal("8.00")
        self.branch.delivery_max_fee = Decimal("10.00")
        result = self.service.estimate("restaurante", self.request(), None)
        self.assertTrue(result.serviceable)
        self.assertEqual(result.delivery_fee, 10.0)

    def test_max_distance_rejects_delivery_area(self):
        self.business_hours = [open_period(40, 60)]
        self.branch.delivery_max_distance_km = Decimal("4.00")
        result = self.service.estimate("restaurante", self.request(), None)
        self.assertFalse(result.serviceable)
        self.assertEqual(result.reason, "outside_delivery_area")
        self.assertEqual(result.distance_km, 4.2)

    def test_business_hour_prep_time_is_used_for_current_day(self):
        self.business_hours = [open_period(25, 35)]

        result = self.service.estimate("restaurante", self.request(), None)
        self.assertTrue(result.serviceable)
        self.assertEqual(result.prep_time_min, 25)
        self.assertEqual(result.prep_time_max, 35)
        self.assertEqual(result.travel_time_min, 18)
        self.assertEqual(result.eta_min, 43)
        self.assertEqual(result.eta_max, 53)

    def test_current_business_hour_without_prep_time_is_not_serviceable(self):
        # A faixa que contem o agora nao tem tempo de preparo. A outra faixa
        # do dia NAO e usada como substituta: era esse fallback que fazia o
        # pedido das 3h sair com o prazo do almoco.
        self.business_hours = [
            open_period(None, None, time(0, 0), time(23, 59, 59, 999999)),
            open_period(25, 35),
        ]

        result = self.service.estimate("restaurante", self.request(), None)
        self.assertFalse(result.serviceable)
        self.assertEqual(result.reason, "prep_time_unavailable")
        self.assertIsNone(result.prep_time_min)
        self.assertIsNone(result.prep_time_max)

    def test_branch_without_business_hours_today_is_closed(self):
        # Sem nenhuma faixa cadastrada para hoje a filial esta fechada, e o
        # motivo agora diz isso em vez de falar de tempo de preparo.
        result = self.service.estimate("restaurante", self.request(), None)
        self.assertFalse(result.serviceable)
        self.assertEqual(result.reason, "branch_closed")
        self.assertIsNone(result.travel_time_min)
        self.assertIsNone(result.eta_min)
        self.assertIsNone(result.eta_max)

    def test_missing_delivery_fee_config_without_default_is_not_serviceable(self):
        # `self.settings` nao tem default_delivery_fee: e o restaurante que
        # nunca configurou o valor de contingencia.
        self.business_hours = [open_period(40, 60)]
        self.branch.delivery_base_fee = None

        result = self.service.estimate("restaurante", self.request(), None)
        self.assertFalse(result.serviceable)
        self.assertEqual(result.reason, "delivery_fee_config_unavailable")
        self.assertEqual(result.distance_km, 4.2)
        self.assertIsNone(result.delivery_fee)

    def test_google_failure_without_default_is_not_serviceable(self):
        self.business_hours = [open_period(40, 60)]
        self.service.maps_client = FakeMapsClient(unavailable=True)

        result = self.service.estimate("restaurante", self.request(), None)
        self.assertFalse(result.serviceable)
        self.assertTrue(result.fallback)
        self.assertEqual(result.provider, "configured_fallback")
        self.assertEqual(result.reason, "route_unavailable")
        self.assertIsNone(result.distance_km)
        self.assertIsNone(result.travel_time_min)
        self.assertIsNone(result.delivery_fee)
        self.assertEqual(result.prep_time_min, 40)
        self.assertEqual(result.prep_time_max, 60)
        self.assertEqual(result.eta_min, 40)
        self.assertEqual(result.eta_max, 60)


class DefaultDeliveryFeeFallbackTests(DeliveryEstimateTests):
    """`restaurant_settings.default_delivery_fee` como taxa de contingencia.

    Antes desta regra o campo era editavel pelo PATCH /admin/settings e nao
    era lido por caminho nenhum: uma queda do Google derrubava TODO pedido de
    entrega da plataforma, com o provider ja se chamando
    "configured_fallback" e nenhum fallback configurado atras dele.

    Herda de DeliveryEstimateTests so pelo setUp; os testes herdados rodam de
    novo aqui, o que e barato e confirma que ligar o campo nao muda o caminho
    normal — `self.settings` continua sem `default_delivery_fee` ate cada
    teste definir o seu.
    """

    def test_google_failure_falls_back_to_the_configured_fee(self):
        self.business_hours = [open_period(40, 60)]
        self.settings.default_delivery_fee = Decimal("9.00")
        self.service.maps_client = FakeMapsClient(unavailable=True)

        result = self.service.estimate("restaurante", self.request(), None)

        self.assertTrue(result.serviceable)
        self.assertEqual(result.delivery_fee, 9.0)
        self.assertTrue(result.fallback)
        self.assertEqual(result.provider, "configured_fallback")
        self.assertIsNone(result.reason)
        # Sem rota nao ha distancia nem tempo de deslocamento; o ETA fica so
        # com o preparo. E o unico numero honesto possivel aqui.
        self.assertIsNone(result.distance_km)
        self.assertIsNone(result.travel_time_min)
        self.assertEqual(result.eta_min, 40)
        self.assertEqual(result.eta_max, 60)

    def test_branch_without_fee_config_falls_back_to_the_configured_fee(self):
        # A rota existe, so falta a regra de preco da filial. A distancia
        # continua conhecida, entao a area de entrega segue sendo conferida.
        self.business_hours = [open_period(40, 60)]
        self.branch.delivery_base_fee = None
        self.settings.default_delivery_fee = Decimal("7.50")

        result = self.service.estimate("restaurante", self.request(), None)

        self.assertTrue(result.serviceable)
        self.assertEqual(result.delivery_fee, 7.5)
        self.assertTrue(result.fallback)
        self.assertEqual(result.provider, "configured_fallback")
        self.assertEqual(result.distance_km, 4.2)
        self.assertEqual(result.travel_time_min, 18)

    def test_max_distance_is_still_enforced_when_the_route_is_known(self):
        self.business_hours = [open_period(40, 60)]
        self.branch.delivery_base_fee = None
        self.branch.delivery_max_distance_km = Decimal("4.00")
        self.settings.default_delivery_fee = Decimal("7.50")

        result = self.service.estimate("restaurante", self.request(), None)

        self.assertFalse(result.serviceable)
        self.assertEqual(result.reason, "outside_delivery_area")

    def test_zero_disables_the_fallback_instead_of_meaning_free_delivery(self):
        # A coluna tem default 0 e a maior parte das linhas em producao nunca
        # foi tocada. Ler esse 0 como escolha faria uma queda do Google virar
        # frete gratis para a plataforma inteira.
        self.business_hours = [open_period(40, 60)]
        self.settings.default_delivery_fee = Decimal("0.00")
        self.service.maps_client = FakeMapsClient(unavailable=True)

        result = self.service.estimate("restaurante", self.request(), None)

        self.assertFalse(result.serviceable)
        self.assertEqual(result.reason, "route_unavailable")
        self.assertIsNone(result.delivery_fee)

    def test_null_disables_the_fallback(self):
        self.business_hours = [open_period(40, 60)]
        self.settings.default_delivery_fee = None
        self.service.maps_client = FakeMapsClient(unavailable=True)

        result = self.service.estimate("restaurante", self.request(), None)

        self.assertFalse(result.serviceable)
        self.assertIsNone(result.delivery_fee)

    def test_the_normal_route_ignores_the_default_fee(self):
        # Com a regra da filial disponivel, o valor de contingencia nao tem
        # nada a ver com o preco cobrado.
        self.business_hours = [open_period(40, 60)]
        self.settings.default_delivery_fee = Decimal("99.00")

        result = self.service.estimate("restaurante", self.request(), None)

        self.assertTrue(result.serviceable)
        self.assertEqual(result.delivery_fee, 11.3)
        self.assertFalse(result.fallback)
        self.assertEqual(result.provider, "google_routes")

    def test_address_id_requires_authentication(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.estimate(
                "restaurante",
                DeliveryEstimateRequest(address_id=uuid.uuid4()),
                None,
            )
        self.assertEqual(raised.exception.status_code, 401)

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
