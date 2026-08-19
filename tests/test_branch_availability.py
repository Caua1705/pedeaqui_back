"""`POST /restaurants/{slug}/branches/availability` — a tela de escolha de filial.

O que estes testes protegem, em ordem de quanto custaria descobrir tarde:

1. **O numero de chamadas pagas ao Google.** E a unica rota publica cujo custo
   cresce com o cadastro do restaurante. Duas asserções aqui contam chamadas
   em vez de olhar resposta: uma trava o geocode em UM por requisicao (e nao
   um por filial), outra trava o filtro geometrico que evita a rota da filial
   provadamente fora do raio. As duas falham em silencio na producao — a conta
   do Maps sobe no mes seguinte e ninguem liga uma coisa a outra.
2. **`is_open_now` calculado aqui.** E a razao de a rota existir: o app parou
   de refazer a conta de intervalo (armadilha 10).
3. **A regra de taxa nao foi duplicada.** O valor da lista tem que ser o mesmo
   que `POST /delivery/estimate` devolve, porque e literalmente a mesma
   funcao — o teste confere o numero contra a regra da filial.

Sem marcador `db`: no padrao de `test_delivery_estimate.py`, o Google e o
banco sao dublados e a regra de negocio (horario, taxa) roda de verdade.
"""

import unittest
import uuid
from datetime import time
from decimal import Decimal
from types import SimpleNamespace

from src.integrations.google_maps_routes_client import (
    Coordinates,
    GoogleMapsLocationNotFoundError,
    GoogleMapsUnavailableError,
    RouteMetrics,
)
from src.schemas.branch_availability_schema import BranchAvailabilityRequest
from src.schemas.delivery_schema import DeliveryAddressInput
from src.services.branch_availability_service import BranchAvailabilityService
from src.services.branch_hours_service import BranchHoursService
from src.services.delivery_estimate_service import DeliveryEstimateService
from src.services.restaurant_service import RestaurantService
from src.utils.geo import haversine_km


DIA_INTEIRO = (time(0, 0), time(23, 59))


def faixa(opens_at=time(0, 0), closes_at=time(23, 59), prep_min=30, prep_max=45):
    return SimpleNamespace(
        weekday=0,
        opens_at=opens_at,
        closes_at=closes_at,
        prep_time_min=prep_min,
        prep_time_max=prep_max,
        is_closed=False,
    )


class FakeMapsClient:
    """Conta as duas operacoes pagas separadamente — e o que os testes medem."""

    def __init__(self) -> None:
        self.route_calls = 0
        self.geocode_calls = 0

    def compute_route(self, origin, destination):
        self.route_calls += 1
        return RouteMetrics(distance_km=4.2, travel_time_min=18)

    def geocode(self, address):
        self.geocode_calls += 1
        return Coordinates(latitude=-3.75, longitude=-38.55)


class FakeCache:
    def __init__(self) -> None:
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


def filial(
    nome: str,
    *,
    latitude,
    longitude,
    is_main=False,
    raio="10.00",
    is_open=True,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=nome,
        display_name=None,
        slug=nome.lower(),
        address=f"Rua {nome}, 1",
        address_street=None,
        address_number=None,
        address_neighborhood=None,
        address_city=None,
        address_state=None,
        address_zipcode=None,
        neighborhood="Centro",
        city="Fortaleza",
        state="CE",
        zipcode=None,
        phone=None,
        whatsapp=None,
        latitude=Decimal(str(latitude)),
        longitude=Decimal(str(longitude)),
        is_main=is_main,
        delivery_base_fee=Decimal("5.00"),
        delivery_fee_per_km=Decimal("1.50"),
        delivery_min_fee=None,
        delivery_max_fee=None,
        delivery_max_distance_km=Decimal(raio),
        # A operacao e da filial desde a revisao 20260818_0025. `is_open` e a
        # pausa manual; os nulos abaixo dizem que esta filial herda os
        # padroes comerciais do restaurante.
        is_open=is_open,
        accepts_delivery=True,
        accepts_pickup=True,
        min_order_value=None,
        service_fee_enabled=None,
        service_fee_amount=None,
        estimated_delivery_time_min=None,
        estimated_delivery_time_max=None,
        default_delivery_fee=None,
    )


class BranchAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.restaurant = SimpleNamespace(id=uuid.uuid4(), slug="junior-da-picanha")

        # O cliente esta em (-3,75; -38,55). A Matriz fica perto. A Centro
        # fica 0,10 grau de latitude ao sul (~11,1 km) mais 0,03 de longitude
        # (~3,3 km) — ~11,6 km em linha reta contra um raio de 10 km. E a
        # filial que o filtro geometrico tem que descartar sem gastar rota.
        self.matriz = filial("Matriz", latitude=-3.7300, longitude=-38.5200, is_main=True)
        self.centro = filial("Centro", latitude=-3.8500, longitude=-38.5200)
        self.filiais = [self.matriz, self.centro]

        self.horarios = {self.matriz.id: [faixa()], self.centro.id: [faixa()]}
        self.maps = FakeMapsClient()

        branch_repository = SimpleNamespace(
            list_active_by_restaurant=lambda restaurant_id: self.filiais,
            get_default_branch=lambda restaurant_id: self.filiais[0] if self.filiais else None,
            get_active_by_id_and_restaurant=lambda branch_id, restaurant_id: next(
                (b for b in self.filiais if b.id == branch_id), None
            ),
            list_business_hours_by_weekday=lambda branch_id, weekday: self.horarios.get(branch_id, []),
        )
        hours_service = BranchHoursService.__new__(BranchHoursService)
        hours_service.branch_repository = branch_repository

        delivery = DeliveryEstimateService.__new__(DeliveryEstimateService)
        delivery.restaurant_service = SimpleNamespace(
            get_active_restaurant=lambda slug: self.restaurant
        )
        delivery.branch_repository = branch_repository
        delivery.branch_hours_service = hours_service
        delivery.customer_repository = SimpleNamespace(get_address=lambda *_: None)
        menu_repository = SimpleNamespace(
            get_settings=lambda restaurant_id: SimpleNamespace(
                min_order_value=None,
                service_fee_enabled=None,
                service_fee_amount=None,
                estimated_delivery_time_min=None,
                estimated_delivery_time_max=None,
                default_delivery_fee=None,
            )
        )
        delivery.menu_repository = menu_repository
        delivery.maps_client = self.maps
        delivery.cache = FakeCache()

        self.service = BranchAvailabilityService.__new__(BranchAvailabilityService)
        self.service.restaurant_service = SimpleNamespace(
            get_active_restaurant=lambda slug: self.restaurant
        )
        self.service.branch_repository = branch_repository
        self.service.menu_repository = menu_repository
        self.service.branch_hours_service = hours_service
        self.service.delivery_service = delivery

    # -----------------------------------------------------------------

    def endereco(self, *, com_coordenada=True):
        return DeliveryAddressInput(
            street="Travessa Joao Felipe",
            number="111",
            neighborhood="Mousa Brasil",
            city="Fortaleza",
            state="CE",
            latitude=Decimal("-3.7500000") if com_coordenada else None,
            longitude=Decimal("-38.5500000") if com_coordenada else None,
        )

    def responder(self, payload: BranchAvailabilityRequest):
        return self.service.list_availability("junior-da-picanha", payload, None)

    # -----------------------------------------------------------------
    # Sem endereco
    # -----------------------------------------------------------------

    def test_sem_endereco_nao_gasta_nenhuma_chamada_ao_google(self):
        resposta = self.responder(BranchAvailabilityRequest())

        self.assertEqual(self.maps.route_calls, 0)
        self.assertEqual(self.maps.geocode_calls, 0)
        self.assertFalse(resposta.address_provided)

    def test_sem_endereco_o_bloco_de_entrega_vem_nulo_e_nao_falso(self):
        """`None` significa "nao perguntei". Um `delivers_to_address=False`
        aqui faria a tela desabilitar filial que entrega perfeitamente."""
        resposta = self.responder(BranchAvailabilityRequest())

        self.assertEqual([item.delivery for item in resposta.branches], [None, None])

    def test_a_pausa_de_uma_filial_nao_alcanca_a_outra(self):
        """O motivo do passo 2 inteiro.

        Enquanto o "fechar agora" foi do restaurante, pausar uma loja pausava
        a rede — e nao havia teste capaz de falhar, porque havia um valor so.
        Aqui as duas filiais dividem o mesmo restaurante e a mesma agenda.
        """
        self.centro.is_open = False

        resposta = self.responder(BranchAvailabilityRequest())
        por_nome = {item.name: item for item in resposta.branches}

        self.assertFalse(por_nome["Centro"].is_open_now)
        self.assertTrue(por_nome["Matriz"].is_open_now)

    def test_pausada_dentro_do_horario_diz_qual_das_duas_fechou(self):
        """`current_period` preenchido com `is_open_now` falso NAO e bug.

        A agenda esta em ordem e quem fechou foi o balcao. Sao duas coisas
        diferentes e a tela escreve textos diferentes para cada uma: uma passa
        sozinha quando o relogio virar, a outra so quando alguem reabrir.
        """
        self.centro.is_open = False

        item = self._item("Centro")

        self.assertFalse(item.is_open_now)
        self.assertEqual(item.closed_reason, "branch_paused")
        self.assertIsNotNone(item.current_period)

    def test_fora_do_horario_o_motivo_e_a_agenda_mesmo_pausada(self):
        """Fechada pelos dois lados reporta a AGENDA.

        `current_period` ja sai nulo aqui; dizer "pausada" faria a tela
        afirmar que a agenda esta em ordem enquanto o campo dela vem vazio.
        Os dois campos contam a mesma historia ou nao contam nenhuma.
        """
        self.horarios[self.centro.id] = [faixa(opens_at=time(3, 0), closes_at=time(3, 1))]
        self.centro.is_open = False

        item = self._item("Centro")

        self.assertFalse(item.is_open_now)
        self.assertEqual(item.closed_reason, "outside_business_hours")
        self.assertIsNone(item.current_period)

    def test_aberta_nao_tem_motivo(self):
        item = self._item("Matriz")

        self.assertTrue(item.is_open_now)
        self.assertIsNone(item.closed_reason)

    def test_filial_pausada_tambem_nao_entrega(self):
        """O bloco de entrega concorda com `is_open_now`.

        Sem isso a tela mostraria a loja fechada e uma taxa de entrega ao
        lado dela — e o cliente tentaria fechar o pedido.

        A Matriz, e nao a Centro: a Centro ja e descartada pelo filtro
        geometrico antes de a estimativa rodar, e o teste mediria o filtro em
        vez da pausa.
        """
        self.matriz.is_open = False

        item = self._item("Matriz", payload=BranchAvailabilityRequest(address=self.endereco()))

        self.assertFalse(item.delivery.delivers_to_address)
        self.assertEqual(item.delivery.reason, "branch_closed")

    def _item(self, nome, payload=None):
        resposta = self.responder(payload or BranchAvailabilityRequest())
        return {item.name: item for item in resposta.branches}[nome]

    def test_aberta_agora_sai_calculado_do_backend(self):
        self.horarios[self.centro.id] = [faixa(opens_at=time(3, 0), closes_at=time(3, 1))]

        resposta = self.responder(BranchAvailabilityRequest())
        por_nome = {item.name: item for item in resposta.branches}

        self.assertTrue(por_nome["Matriz"].is_open_now)
        self.assertEqual(por_nome["Matriz"].current_period.opens_at, DIA_INTEIRO[0])
        self.assertFalse(por_nome["Centro"].is_open_now)
        self.assertIsNone(por_nome["Centro"].current_period)

    def test_default_branch_id_e_a_filial_principal(self):
        resposta = self.responder(BranchAvailabilityRequest())

        self.assertEqual(resposta.default_branch_id, self.matriz.id)

    # -----------------------------------------------------------------
    # Com endereco
    # -----------------------------------------------------------------

    def test_com_endereco_a_taxa_sai_pela_regra_da_filial(self):
        """4,2 km * 1,50 + 5,00 = 11,30. E o mesmo numero que
        `POST /delivery/estimate` devolve, porque e a mesma funcao."""
        resposta = self.responder(BranchAvailabilityRequest(address=self.endereco()))
        matriz = next(item for item in resposta.branches if item.name == "Matriz")

        self.assertTrue(resposta.address_provided)
        self.assertTrue(matriz.delivery.delivers_to_address)
        self.assertEqual(matriz.delivery.delivery_fee, 11.3)
        self.assertEqual(matriz.delivery.distance_km, 4.2)
        self.assertEqual(matriz.delivery.eta_min, 48)

    def test_filial_fora_do_raio_em_linha_reta_nao_gasta_rota(self):
        """O filtro geometrico. A Centro esta a ~11 km em linha reta com raio
        de 10, entao a rota dirigida so pode ser maior — nao ha o que
        perguntar ao Google."""
        resposta = self.responder(BranchAvailabilityRequest(address=self.endereco()))
        centro = next(item for item in resposta.branches if item.name == "Centro")

        self.assertFalse(centro.delivery.delivers_to_address)
        self.assertEqual(centro.delivery.reason, "outside_delivery_area")
        self.assertIsNone(centro.delivery.distance_km)
        # Uma rota, nao duas: a da Matriz.
        self.assertEqual(self.maps.route_calls, 1)

    def test_o_geocode_acontece_uma_vez_para_todas_as_filiais(self):
        """O defeito que este teste existe para impedir: resolver o endereco
        dentro do laco faria N filiais virarem N geocodes do MESMO lugar."""
        self.responder(
            BranchAvailabilityRequest(address=self.endereco(com_coordenada=False))
        )

        self.assertEqual(self.maps.geocode_calls, 1)

    def test_filial_sem_raio_configurado_nao_e_descartada_pela_linha_reta(self):
        """"Nao sei" tem que virar chamada ao Google, nunca recusa."""
        self.centro.delivery_max_distance_km = None

        resposta = self.responder(BranchAvailabilityRequest(address=self.endereco()))
        centro = next(item for item in resposta.branches if item.name == "Centro")

        self.assertTrue(centro.delivery.delivers_to_address)
        self.assertEqual(self.maps.route_calls, 2)

    def test_filial_sem_coordenada_nao_e_descartada_pela_linha_reta(self):
        self.centro.latitude = None
        self.centro.longitude = None

        self.responder(BranchAvailabilityRequest(address=self.endereco()))

        self.assertEqual(self.maps.route_calls, 2)

    def test_filial_fechada_reporta_o_motivo_no_bloco_de_entrega(self):
        """Fechada nao devolve taxa: `estimate` recusa antes de calcular rota.
        A tela tem `is_open_now` para isso e nao depende deste campo."""
        # Na Matriz, e nao na Centro: a Centro esta fora do raio, e o filtro
        # geometrico responde ANTES de a filial ser consultada por horario.
        self.horarios[self.matriz.id] = []

        resposta = self.responder(BranchAvailabilityRequest(address=self.endereco()))
        matriz = next(item for item in resposta.branches if item.name == "Matriz")

        self.assertFalse(matriz.is_open_now)
        self.assertFalse(matriz.delivery.delivers_to_address)
        self.assertEqual(matriz.delivery.reason, "branch_closed")
        self.assertIsNone(matriz.delivery.delivery_fee)

    # -----------------------------------------------------------------
    # Quando o Google cai
    # -----------------------------------------------------------------

    def test_google_fora_do_ar_nao_derruba_a_lista(self):
        """A parte que interessa primeiro — quais filiais existem e quais
        estao abertas — nao depende de Google nenhum. Deixar a excecao do
        geocode subir daria 500 na tela inteira."""
        def cair(address):
            raise GoogleMapsUnavailableError()

        self.maps.geocode = cair

        resposta = self.responder(
            BranchAvailabilityRequest(address=self.endereco(com_coordenada=False))
        )

        self.assertEqual(len(resposta.branches), 2)
        self.assertTrue(resposta.branches[0].is_open_now)
        self.assertTrue(resposta.address_provided)
        self.assertEqual(resposta.branches[0].delivery.reason, "route_unavailable")
        self.assertFalse(resposta.branches[0].delivery.delivers_to_address)

    def test_endereco_que_o_google_nao_localiza_tem_motivo_proprio(self):
        """Separado de `route_unavailable` porque a acao do front e outra:
        aqui quem corrige e o cliente, revendo o que digitou."""
        def nao_achar(address):
            raise GoogleMapsLocationNotFoundError()

        self.maps.geocode = nao_achar

        resposta = self.responder(
            BranchAvailabilityRequest(address=self.endereco(com_coordenada=False))
        )

        self.assertEqual(
            [item.delivery.reason for item in resposta.branches],
            ["address_not_found", "address_not_found"],
        )

    def test_falha_de_geocode_nao_gasta_rota(self):
        def cair(address):
            raise GoogleMapsUnavailableError()

        self.maps.geocode = cair
        self.responder(BranchAvailabilityRequest(address=self.endereco(com_coordenada=False)))

        self.assertEqual(self.maps.route_calls, 0)

    def test_restaurante_sem_filial_ativa_responde_lista_vazia(self):
        self.filiais = []

        resposta = self.responder(BranchAvailabilityRequest())

        self.assertEqual(resposta.branches, [])
        self.assertIsNone(resposta.default_branch_id)


class RequestValidationTests(unittest.TestCase):
    def test_corpo_vazio_e_valido(self):
        payload = BranchAvailabilityRequest()

        self.assertIsNone(payload.address_id)
        self.assertIsNone(payload.address)

    def test_os_dois_jeitos_de_informar_endereco_sao_excludentes(self):
        with self.assertRaises(ValueError):
            BranchAvailabilityRequest(
                address_id=uuid.uuid4(),
                address=DeliveryAddressInput(
                    street="Rua", number="1", neighborhood="Centro"
                ),
            )


class HaversineTests(unittest.TestCase):
    """A funcao so tem uma responsabilidade: nunca superestimar a distancia
    dirigida. Se ela superestimar, o filtro descarta filial que entrega."""

    def test_um_grau_de_latitude_da_cerca_de_111_km(self):
        distancia = haversine_km(0.0, 0.0, 1.0, 0.0)

        self.assertAlmostEqual(distancia, 111.19, places=1)

    def test_a_mesma_coordenada_da_zero(self):
        self.assertEqual(haversine_km(-3.73, -38.52, -3.73, -38.52), 0.0)

    def test_e_simetrica(self):
        ida = haversine_km(-3.73, -38.52, -3.83, -38.52)
        volta = haversine_km(-3.83, -38.52, -3.73, -38.52)

        self.assertEqual(ida, volta)


class RotaTests(unittest.TestCase):
    """A camada HTTP, contra o `main.app` de verdade.

    Contra o app real e nao contra um `FastAPI()` montado aqui de proposito:
    a rota leva `@limiter.limit`, e o wrapper dele le
    `request.state.view_rate_limit`. Sem o `RateLimitStateMiddleware` isso
    estoura `AttributeError` e vira 500 — um app de teste montado a mao
    passaria por cima justamente do acoplamento que se quer conferir.
    """

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from main import app
        from src.api.dependencies.database import get_db

        self.app = app
        self.app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()

    def _responder_com(self, resposta):
        """Substitui o service inteiro: esta classe testa transporte, nao regra."""
        from src.api.endpoints import branches as rota

        original = rota.BranchAvailabilityService
        rota.BranchAvailabilityService = lambda db: SimpleNamespace(
            list_availability=lambda *args: resposta
        )
        self.addCleanup(setattr, rota, "BranchAvailabilityService", original)

    def test_a_rota_esta_registrada(self):
        caminhos = {rota.path for rota in self.app.routes if hasattr(rota, "path")}

        self.assertIn("/restaurants/{restaurant_slug}/branches/availability", caminhos)

    def test_corpo_vazio_responde_200(self):
        from src.schemas.branch_availability_schema import BranchAvailabilityResponse

        self._responder_com(
            BranchAvailabilityResponse(
                restaurant_slug="junior-da-picanha",
                address_provided=False,
                default_branch_id=None,
                branches=[],
            )
        )

        resposta = self.client.post(
            "/restaurants/junior-da-picanha/branches/availability", json={}
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(resposta.json()["address_provided"])

    def test_os_dois_enderecos_juntos_respondem_422(self):
        resposta = self.client.post(
            "/restaurants/junior-da-picanha/branches/availability",
            json={
                "address_id": str(uuid.uuid4()),
                "address": {"street": "Rua", "number": "1", "neighborhood": "Centro"},
            },
        )

        self.assertEqual(resposta.status_code, 422)

    def test_campo_desconhecido_no_corpo_responde_422(self):
        """`extra="forbid"`: corpo com campo que a rota nao conhece e erro do
        front, e falhar alto aqui e mais barato que ignorar em silencio."""
        resposta = self.client.post(
            "/restaurants/junior-da-picanha/branches/availability",
            json={"branch_id": str(uuid.uuid4())},
        )

        self.assertEqual(resposta.status_code, 422)


class BuildAddressReuseTests(unittest.TestCase):
    """O endereco exibido sai do MESMO builder do /restaurants/{slug}/info."""

    def test_endereco_montado_pelo_builder_do_restaurant_service(self):
        item = RestaurantService._build_address(
            filial("Matriz", latitude=-3.73, longitude=-38.52)
        )

        self.assertEqual(item.neighborhood, "Centro")
        self.assertIn("Fortaleza - CE", item.full_address)
