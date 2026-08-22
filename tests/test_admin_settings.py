"""Configuracoes do painel (BLOCO C da Fase 3).

O que estes testes protegem:

1. `platform_commission_percent` nao entra no contrato do lojista, nem para
   leitura. E o percentual que a plataforma cobra; virar campo de tela e
   virar campo editavel.
2. Filial no path nao autoriza nada. Um manager preso a uma filial recebe
   404 na filial vizinha do mesmo restaurante, e qualquer um recebe 404 na
   filial de outro restaurante.
3. Validacao que so faz sentido em par (minimo x maximo, faixas que se
   sobrepoem) roda sobre a MESCLA com o banco — mandar so o maximo nao pode
   deixar a filial com teto abaixo do piso.
"""

import unittest
import uuid
from datetime import time
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException

from src.api.dependencies.admin_scope import AdminScope
from src.schemas.admin_settings_schema import (
    AdminBranchOrderTypesRequest,
    AdminBranchSettingsUpdate,
    AdminBranchUpdate,
    AdminPaymentMethodCreate,
    AdminPaymentMethodUpdate,
    AdminRestaurantSettingsResponse,
    AdminRestaurantSettingsUpdate,
    BranchPrepTimeAdjustRequest,
    BusinessHourInput,
    BusinessHoursReplaceRequest,
    StoreStatusRequest,
)
from src.services.admin_settings_service import AdminSettingsService


RESTAURANT_ID = uuid.uuid4()
OTHER_RESTAURANT_ID = uuid.uuid4()


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


def make_settings(**overrides):
    values = {
        "restaurant_id": RESTAURANT_ID,
        "min_order_value": Decimal("20.00"),
        "estimated_delivery_time_min": 30,
        "estimated_delivery_time_max": 60,
        "default_delivery_fee": Decimal("5.00"),
        "free_delivery_enabled": None,
        "free_delivery_min_order_value": None,
        "service_fee_enabled": True,
        "service_fee_amount": Decimal("0.99"),
        "platform_commission_percent": Decimal("10.00"),
        "receipt_footer_message": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_branch(**overrides):
    values = {
        "id": uuid.uuid4(),
        "restaurant_id": RESTAURANT_ID,
        "name": "Centro",
        "slug": "centro",
        "display_name": None,
        "email": None,
        "phone": None,
        "whatsapp": None,
        "address": "Rua A, 100",
        "neighborhood": "Centro",
        "city": "Fortaleza",
        "state": "CE",
        "zipcode": None,
        "latitude": None,
        "longitude": None,
        "delivery_base_fee": Decimal("5.00"),
        "delivery_fee_per_km": Decimal("1.50"),
        "delivery_min_fee": Decimal("5.00"),
        "delivery_max_fee": Decimal("20.00"),
        "delivery_max_distance_km": Decimal("10.00"),
        "is_main": True,
        "is_active": True,
        # A operacao da filial (revisao 20260818_0025). As tres chaves sao
        # NOT NULL; os seis campos comerciais nascem nulos, e nulo significa
        # "herda o padrao do restaurante".
        "is_open": True,
        "accepts_delivery": True,
        "accepts_pickup": True,
        "delivery_paused_until": None,
        "delivery_pause_reason": None,
        "min_order_value": None,
        "service_fee_enabled": None,
        "service_fee_amount": None,
        "estimated_delivery_time_min": None,
        "estimated_delivery_time_max": None,
        "default_delivery_fee": None,
        "free_delivery_enabled": None,
        "free_delivery_min_order_value": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_business_hour(**overrides):
    values = {
        "id": uuid.uuid4(),
        "branch_id": uuid.uuid4(),
        "weekday": 0,
        "opens_at": time(11, 0),
        "closes_at": time(15, 0),
        "prep_time_min": 30,
        "prep_time_max": 45,
        "is_closed": False,
        "sort_order": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_payment_method(**overrides):
    values = {
        "id": uuid.uuid4(),
        "branch_id": uuid.uuid4(),
        "payment_flow": "delivery",
        "method_type": "cash",
        "brand": None,
        "label": "Dinheiro",
        "icon_key": None,
        "enabled": True,
        "requires_gateway": False,
        "sort_order": 0,
        "notes": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeSettingsRepository:
    def __init__(self, settings=None, payment_methods=()):
        self.settings = settings
        self.payment_methods = list(payment_methods)
        self.business_hours = []
        self.deleted_hours_for = []
        self.deleted_methods = []
        self.added = []

    def get_settings(self, restaurant_id):
        if self.settings is not None and self.settings.restaurant_id == restaurant_id:
            return self.settings
        return None

    def add_settings(self, settings):
        self.settings = settings
        self.added.append(settings)
        return settings

    def delete_business_hours(self, branch_id):
        self.deleted_hours_for.append(branch_id)
        self.business_hours = [
            period for period in self.business_hours if period.branch_id != branch_id
        ]

    def add_business_hours(self, periods):
        for position, period in enumerate(periods):
            period.id = uuid.uuid4()
            self.business_hours.append(period)
        return periods

    def list_payment_methods(self, branch_id):
        return [item for item in self.payment_methods if item.branch_id == branch_id]

    def get_payment_method(self, method_id, restaurant_id):
        for item in self.payment_methods:
            if item.id == method_id and getattr(item, "restaurant_id", RESTAURANT_ID) == restaurant_id:
                return item
        return None

    def get_payment_method_by_type(self, branch_id, payment_flow, method_type, brand):
        for item in self.payment_methods:
            if (
                item.branch_id == branch_id
                and item.payment_flow == payment_flow
                and item.method_type == method_type
                and item.brand == brand
            ):
                return item
        return None

    def add_payment_method(self, method):
        method.id = uuid.uuid4()
        self.payment_methods.append(method)
        self.added.append(method)
        return method

    def delete_payment_method(self, method):
        self.deleted_methods.append(method)
        self.payment_methods.remove(method)


class FakeBranchRepository:
    """Respeita o filtro por restaurante, como o WHERE real."""

    def __init__(self, branches=(), business_hours=()):
        self.branches = list(branches)
        self.business_hours = list(business_hours)

    def get_active_by_id_and_restaurant(self, branch_id, restaurant_id):
        for branch in self.branches:
            if branch.id == branch_id and branch.restaurant_id == restaurant_id:
                return branch
        return None

    def list_active_by_restaurant(self, restaurant_id):
        return [branch for branch in self.branches if branch.restaurant_id == restaurant_id]

    def list_business_hours(self, branch_id):
        return [period for period in self.business_hours if period.branch_id == branch_id]


class FakeBranchHoursService:
    """Faixa vigente, ja resolvida.

    A escolha da faixa e testada em test_branch_hours.py; aqui o que
    importa e o que o ajuste faz DEPOIS de saber qual faixa esta valendo,
    incluindo o caso em que nao ha nenhuma.
    """

    def __init__(self, current_period=None):
        self.current_period = current_period

    def find_current_period(self, branch_id, now=None):
        if self.current_period is None or self.current_period.branch_id != branch_id:
            return None
        return self.current_period


def build_service(settings_repository=None, branch_repository=None, current_period=None):
    service = AdminSettingsService(FakeDb())
    service.repository = settings_repository or FakeSettingsRepository()
    service.branch_repository = branch_repository or FakeBranchRepository()
    service.branch_hours_service = FakeBranchHoursService(current_period)
    return service


def scope(restaurant_id=RESTAURANT_ID, branch_id=None):
    return AdminScope(admin_user=None, restaurant_id=restaurant_id, branch_id=branch_id)


class RestaurantSettingsTests(unittest.TestCase):
    def test_commission_is_not_part_of_the_contract(self):
        # Nem para leitura: publicar no OpenAPI do painel transformaria o
        # percentual da plataforma em campo de tela.
        self.assertNotIn(
            "platform_commission_percent", AdminRestaurantSettingsResponse.model_fields
        )
        self.assertNotIn(
            "platform_commission_percent", AdminRestaurantSettingsUpdate.model_fields
        )

    def test_settings_row_is_created_on_the_first_visit(self):
        repository = FakeSettingsRepository(settings=None)
        response = build_service(repository).get_restaurant_settings(scope())

        # A linha e opcional no schema; sem cria-la sob demanda o painel
        # responderia 404 para um restaurante que funciona normalmente.
        self.assertEqual(len(repository.added), 1)
        self.assertIsInstance(response, AdminRestaurantSettingsResponse)

    def test_partial_update_touches_only_what_was_sent(self):
        settings_row = make_settings(
            min_order_value=Decimal("20.00"), service_fee_amount=Decimal("0.99")
        )
        repository = FakeSettingsRepository(settings=settings_row)
        build_service(repository).update_restaurant_settings(
            scope(), AdminRestaurantSettingsUpdate(min_order_value=Decimal("35.00"))
        )

        self.assertEqual(settings_row.min_order_value, Decimal("35.00"))
        self.assertEqual(settings_row.service_fee_amount, Decimal("0.99"))

    def test_the_day_switches_left_the_restaurant_contract(self):
        """`is_open`, `accepts_delivery` e `accepts_pickup` nao tem padrao.

        Eles sao o estado do dia de UMA loja. Enquanto moravam aqui, fechar a
        filial do Centro fechava a da Aldeota junto — e o schema de volta
        significaria a mesma coisa de volta.
        """
        for campo in ("is_open", "accepts_delivery", "accepts_pickup"):
            self.assertNotIn(campo, AdminRestaurantSettingsResponse.model_fields)
            self.assertNotIn(campo, AdminRestaurantSettingsUpdate.model_fields)

    def test_money_is_stored_with_two_decimals(self):
        settings_row = make_settings()
        repository = FakeSettingsRepository(settings=settings_row)
        build_service(repository).update_restaurant_settings(
            scope(), AdminRestaurantSettingsUpdate(service_fee_amount=Decimal("1.239"))
        )

        self.assertEqual(settings_row.service_fee_amount, Decimal("1.24"))

    def test_delivery_time_range_is_validated_against_the_stored_value(self):
        settings_row = make_settings(estimated_delivery_time_min=40)
        repository = FakeSettingsRepository(settings=settings_row)
        with self.assertRaises(HTTPException) as raised:
            build_service(repository).update_restaurant_settings(
                scope(), AdminRestaurantSettingsUpdate(estimated_delivery_time_max=20)
            )

        # 20 e valido sozinho; impossivel para um minimo de 40 ja gravado.
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(settings_row.estimated_delivery_time_max, 60)

    def test_settings_of_another_restaurant_are_not_reachable(self):
        settings_row = make_settings(restaurant_id=OTHER_RESTAURANT_ID)
        repository = FakeSettingsRepository(settings=settings_row)
        build_service(repository).get_restaurant_settings(scope())

        # Nao achou a linha do restaurante do token: criou a dele, nao
        # devolveu a alheia.
        self.assertEqual(len(repository.added), 1)
        self.assertEqual(repository.added[0].restaurant_id, RESTAURANT_ID)


class BranchScopeTests(unittest.TestCase):
    def setUp(self):
        self.mine = make_branch(name="Centro")
        self.sibling = make_branch(name="Aldeota")
        self.foreign = make_branch(restaurant_id=OTHER_RESTAURANT_ID)
        self.branch_repository = FakeBranchRepository(
            branches=[self.mine, self.sibling, self.foreign]
        )

    def test_owner_sees_every_branch_of_the_restaurant(self):
        response = build_service(branch_repository=self.branch_repository).list_branches(scope())

        self.assertEqual(
            {item.id for item in response}, {self.mine.id, self.sibling.id}
        )

    def test_branch_bound_user_sees_only_the_own_branch(self):
        response = build_service(branch_repository=self.branch_repository).list_branches(
            scope(branch_id=self.mine.id)
        )

        self.assertEqual([item.id for item in response], [self.mine.id])

    def test_sibling_branch_is_not_found_for_a_branch_bound_user(self):
        with self.assertRaises(HTTPException) as raised:
            build_service(branch_repository=self.branch_repository).get_branch(
                scope(branch_id=self.mine.id), self.sibling.id
            )

        # 404 e nao 403: para quem esta preso a uma filial, as outras nao
        # existem.
        self.assertEqual(raised.exception.status_code, 404)

    def test_branch_of_another_restaurant_is_not_found(self):
        with self.assertRaises(HTTPException) as raised:
            build_service(branch_repository=self.branch_repository).get_branch(
                scope(), self.foreign.id
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_missing_delivery_rule_is_not_reported_as_zero(self):
        branch = make_branch(delivery_fee_per_km=None)
        service = build_service(branch_repository=FakeBranchRepository(branches=[branch]))

        # "sem taxa por km cadastrada" e "taxa por km zero" sao coisas
        # diferentes para quem esta configurando entrega.
        self.assertIsNone(service.get_branch(scope(), branch.id).delivery_fee_per_km)


class BranchUpdateTests(unittest.TestCase):
    def test_delivery_rules_are_validated_against_the_stored_values(self):
        branch = make_branch(delivery_min_fee=Decimal("8.00"), delivery_max_fee=Decimal("20.00"))
        service = build_service(branch_repository=FakeBranchRepository(branches=[branch]))

        with self.assertRaises(HTTPException) as raised:
            service.update_branch(
                scope(), branch.id, AdminBranchUpdate(delivery_max_fee=Decimal("5.00"))
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(branch.delivery_max_fee, Decimal("20.00"))

    def test_valid_partial_update_is_applied(self):
        branch = make_branch(phone=None)
        service = build_service(branch_repository=FakeBranchRepository(branches=[branch]))
        service.update_branch(scope(), branch.id, AdminBranchUpdate(phone="8532000000"))

        self.assertEqual(branch.phone, "8532000000")
        self.assertEqual(branch.address, "Rua A, 100")

    def test_slug_and_is_active_are_not_editable(self):
        # Slug e URL publica; desativar filial some com ela do app de todo
        # mundo e deixa pedido em aberto sem cozinha.
        self.assertNotIn("slug", AdminBranchUpdate.model_fields)
        self.assertNotIn("is_active", AdminBranchUpdate.model_fields)


class BusinessHoursTests(unittest.TestCase):
    def setUp(self):
        self.branch = make_branch()
        self.branch_repository = FakeBranchRepository(branches=[self.branch])
        self.repository = FakeSettingsRepository()

    def service(self):
        return build_service(self.repository, self.branch_repository)

    def test_week_is_replaced_and_positions_follow_the_body(self):
        self.service().replace_business_hours(
            scope(),
            self.branch.id,
            BusinessHoursReplaceRequest(
                periods=[
                    BusinessHourInput(
                        weekday=0, opens_at=time(11, 0), closes_at=time(14, 0)
                    ),
                    BusinessHourInput(
                        weekday=0, opens_at=time(18, 0), closes_at=time(23, 0)
                    ),
                ]
            ),
        )

        self.assertEqual(self.repository.deleted_hours_for, [self.branch.id])
        self.assertEqual(
            [period.sort_order for period in self.repository.business_hours], [0, 1]
        )

    def test_overlapping_periods_are_refused(self):
        with self.assertRaises(HTTPException) as raised:
            self.service().replace_business_hours(
                scope(),
                self.branch.id,
                BusinessHoursReplaceRequest(
                    periods=[
                        BusinessHourInput(
                            weekday=2, opens_at=time(11, 0), closes_at=time(15, 0)
                        ),
                        BusinessHourInput(
                            weekday=2, opens_at=time(14, 0), closes_at=time(23, 0)
                        ),
                    ]
                ),
            )

        # Duas faixas sobrepostas tornam o tempo de preparo imprevisivel:
        # find_current_period devolve a primeira que contem o agora.
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(self.repository.business_hours, [])

    def test_overnight_period_does_not_count_as_overlap(self):
        self.service().replace_business_hours(
            scope(),
            self.branch.id,
            BusinessHoursReplaceRequest(
                periods=[
                    BusinessHourInput(
                        weekday=5, opens_at=time(11, 0), closes_at=time(15, 0)
                    ),
                    BusinessHourInput(
                        weekday=5, opens_at=time(18, 0), closes_at=time(2, 0)
                    ),
                ]
            ),
        )

        # 18:00-02:00 pertence ao dia em que COMECA e nao invade o almoco.
        self.assertEqual(len(self.repository.business_hours), 2)

    def test_closed_day_cannot_have_open_periods(self):
        with self.assertRaises(HTTPException) as raised:
            self.service().replace_business_hours(
                scope(),
                self.branch.id,
                BusinessHoursReplaceRequest(
                    periods=[
                        BusinessHourInput(weekday=6, is_closed=True),
                        BusinessHourInput(
                            weekday=6, opens_at=time(11, 0), closes_at=time(15, 0)
                        ),
                    ]
                ),
            )

        self.assertEqual(raised.exception.status_code, 422)

    def test_empty_body_closes_every_day(self):
        self.service().replace_business_hours(
            scope(), self.branch.id, BusinessHoursReplaceRequest(periods=[])
        )

        # Dia ausente e dia fechado: a semana antiga foi apagada e nada
        # entrou no lugar.
        self.assertEqual(self.repository.deleted_hours_for, [self.branch.id])
        self.assertEqual(self.repository.business_hours, [])

    def test_branch_outside_the_scope_is_refused_before_deleting(self):
        foreign = make_branch(restaurant_id=OTHER_RESTAURANT_ID)
        self.branch_repository.branches.append(foreign)

        with self.assertRaises(HTTPException) as raised:
            self.service().replace_business_hours(
                scope(), foreign.id, BusinessHoursReplaceRequest(periods=[])
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.repository.deleted_hours_for, [])


class BusinessHourContractTests(unittest.TestCase):
    def test_open_period_needs_both_ends(self):
        with self.assertRaises(ValueError):
            BusinessHourInput(weekday=0, opens_at=time(11, 0))

    def test_equal_open_and_close_is_refused(self):
        # A faixa nao cobriria instante nenhum e a filial ficaria "aberta"
        # sem nunca aceitar pedido.
        with self.assertRaises(ValueError):
            BusinessHourInput(weekday=0, opens_at=time(11, 0), closes_at=time(11, 0))

    def test_prep_time_range_is_validated(self):
        with self.assertRaises(ValueError):
            BusinessHourInput(
                weekday=0,
                opens_at=time(11, 0),
                closes_at=time(15, 0),
                prep_time_min=40,
                prep_time_max=20,
            )


class PrepTimeAdjustTests(unittest.TestCase):
    """O atalho de +5/-10 do dia cheio.

    A regra que mais importa aqui e a de ALVO: o ajuste escreve na faixa
    que contem o agora, que e a mesma que a estimativa de entrega le
    (BranchHoursService.find_current_period). Escrever em outra seria mudar
    um numero que o proximo pedido nao consulta.
    """

    def setUp(self):
        self.branch = make_branch()
        self.branch_repository = FakeBranchRepository(branches=[self.branch])
        self.lunch = make_business_hour(
            branch_id=self.branch.id, prep_time_min=30, prep_time_max=45
        )
        self.dinner = make_business_hour(
            branch_id=self.branch.id, sort_order=1, prep_time_min=20, prep_time_max=30
        )

    def service(self, current_period=None):
        return build_service(
            branch_repository=self.branch_repository,
            current_period=self.lunch if current_period is None else current_period,
        )

    def test_delta_shifts_the_whole_window(self):
        response = self.service().adjust_prep_time(
            scope(), self.branch.id, BranchPrepTimeAdjustRequest(delta_minutes=5)
        )

        self.assertEqual((response.prep_time_min, response.prep_time_max), (35, 50))
        self.assertEqual((self.lunch.prep_time_min, self.lunch.prep_time_max), (35, 50))

    def test_only_the_current_period_is_touched(self):
        self.service().adjust_prep_time(
            scope(), self.branch.id, BranchPrepTimeAdjustRequest(delta_minutes=5)
        )

        # O aperto do almoco nao pode virar o prazo padrao do jantar.
        self.assertEqual((self.dinner.prep_time_min, self.dinner.prep_time_max), (20, 30))

    def test_negative_delta_stops_at_zero(self):
        short = make_business_hour(branch_id=self.branch.id, prep_time_min=5, prep_time_max=15)
        response = self.service(short).adjust_prep_time(
            scope(), self.branch.id, BranchPrepTimeAdjustRequest(delta_minutes=-10)
        )

        # Aparar e nao recusar: quem apertou "-10" de novo quer o menor
        # prazo possivel, nao um erro.
        self.assertEqual((response.prep_time_min, response.prep_time_max), (0, 5))

    def test_absolute_values_are_written_as_sent(self):
        empty = make_business_hour(
            branch_id=self.branch.id, prep_time_min=None, prep_time_max=None
        )
        response = self.service(empty).adjust_prep_time(
            scope(),
            self.branch.id,
            BranchPrepTimeAdjustRequest(prep_time_min=25, prep_time_max=40),
        )

        self.assertEqual((response.prep_time_min, response.prep_time_max), (25, 40))

    def test_delta_without_a_stored_base_is_refused(self):
        empty = make_business_hour(
            branch_id=self.branch.id, prep_time_min=None, prep_time_max=None
        )

        with self.assertRaises(HTTPException) as raised:
            self.service(empty).adjust_prep_time(
                scope(), self.branch.id, BranchPrepTimeAdjustRequest(delta_minutes=5)
            )

        # Tratar o nulo como zero transformaria "+5" em "o preparo desta
        # filial agora e de 5 minutos".
        self.assertEqual(raised.exception.status_code, 409)

    def test_closed_branch_has_nothing_to_adjust(self):
        with self.assertRaises(HTTPException) as raised:
            build_service(
                branch_repository=self.branch_repository, current_period=None
            ).adjust_prep_time(
                scope(), self.branch.id, BranchPrepTimeAdjustRequest(delta_minutes=5)
            )

        # Escolher "a faixa mais parecida" foi o bug que BranchHoursService
        # nasceu para corrigir.
        self.assertEqual(raised.exception.status_code, 409)

    def test_branch_outside_the_scope_is_refused(self):
        foreign = make_branch(restaurant_id=OTHER_RESTAURANT_ID)
        self.branch_repository.branches.append(foreign)
        foreign_period = make_business_hour(branch_id=foreign.id)

        with self.assertRaises(HTTPException) as raised:
            self.service(foreign_period).adjust_prep_time(
                scope(), foreign.id, BranchPrepTimeAdjustRequest(delta_minutes=5)
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(foreign_period.prep_time_min, 30)


class PrepTimeContractTests(unittest.TestCase):
    def test_one_mode_at_a_time(self):
        with self.assertRaises(ValueError):
            BranchPrepTimeAdjustRequest(delta_minutes=5, prep_time_min=10, prep_time_max=20)

    def test_empty_body_is_refused(self):
        with self.assertRaises(ValueError):
            BranchPrepTimeAdjustRequest()

    def test_absolute_values_travel_together(self):
        # So o maximo deixaria a faixa com teto abaixo do piso, o mesmo
        # problema que AdminBranchDeliveryRules resolve para as taxas.
        with self.assertRaises(ValueError):
            BranchPrepTimeAdjustRequest(prep_time_max=20)

    def test_inverted_absolute_range_is_refused(self):
        with self.assertRaises(ValueError):
            BranchPrepTimeAdjustRequest(prep_time_min=40, prep_time_max=20)

    def test_delta_is_capped(self):
        # Somar duas horas ao prazo nao e ajuste do dia; e reconfiguracao
        # da semana, que tem rota propria.
        with self.assertRaises(ValueError):
            BranchPrepTimeAdjustRequest(delta_minutes=180)


class PaymentMethodTests(unittest.TestCase):
    def setUp(self):
        self.branch = make_branch()
        self.branch_repository = FakeBranchRepository(branches=[self.branch])

    def test_method_is_created_in_the_branch(self):
        repository = FakeSettingsRepository()
        response = build_service(repository, self.branch_repository).create_payment_method(
            scope(),
            self.branch.id,
            AdminPaymentMethodCreate(
                payment_flow="delivery", method_type="cash", label="Dinheiro"
            ),
        )

        self.assertEqual(response.branch_id, self.branch.id)

    def test_duplicated_method_is_refused(self):
        existing = make_payment_method(branch_id=self.branch.id)
        repository = FakeSettingsRepository(payment_methods=[existing])

        with self.assertRaises(HTTPException) as raised:
            build_service(repository, self.branch_repository).create_payment_method(
                scope(),
                self.branch.id,
                AdminPaymentMethodCreate(
                    payment_flow="delivery", method_type="cash", label="Dinheiro"
                ),
            )

        # Duas linhas iguais apareceriam duas vezes no checkout do cliente.
        self.assertEqual(raised.exception.status_code, 409)

    def test_flow_and_type_cannot_be_changed(self):
        # Trocar o fluxo mudaria, no meio do expediente, como os proximos
        # pedidos daquela filial sao cobrados.
        self.assertNotIn("payment_flow", AdminPaymentMethodUpdate.model_fields)
        self.assertNotIn("method_type", AdminPaymentMethodUpdate.model_fields)

    def test_method_of_a_branch_outside_the_scope_is_not_found(self):
        sibling = make_branch()
        self.branch_repository.branches.append(sibling)
        method = make_payment_method(branch_id=sibling.id)
        repository = FakeSettingsRepository(payment_methods=[method])

        with self.assertRaises(HTTPException) as raised:
            build_service(repository, self.branch_repository).update_payment_method(
                scope(branch_id=self.branch.id),
                method.id,
                AdminPaymentMethodUpdate(enabled=False),
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_method_is_deleted(self):
        method = make_payment_method(branch_id=self.branch.id)
        repository = FakeSettingsRepository(payment_methods=[method])
        build_service(repository, self.branch_repository).delete_payment_method(
            scope(), method.id
        )

        # Seguro apagar: orders.payment_method guarda texto, nao FK.
        self.assertEqual(repository.deleted_methods, [method])


if __name__ == "__main__":
    unittest.main()


class BranchOperationTests(unittest.TestCase):
    """A operacao por filial (revisao 20260818_0025).

    O que estes testes protegem e uma coisa so, dita de varios angulos: a
    pausa de uma filial nao pode chegar em outra. Enquanto `is_open` foi do
    restaurante, ela chegava — e nao havia teste que pudesse falhar, porque
    havia um valor so.
    """

    def setUp(self):
        self.centro = make_branch(name="Centro")
        self.aldeota = make_branch(name="Aldeota", is_main=False)
        self.settings_row = make_settings()
        self.repository = FakeSettingsRepository(settings=self.settings_row)
        self.branches = FakeBranchRepository(branches=[self.centro, self.aldeota])

    def service(self, current_period=None):
        return build_service(
            settings_repository=self.repository,
            branch_repository=self.branches,
            current_period=current_period,
        )

    def test_pausing_one_branch_leaves_the_other_open(self):
        self.service().set_store_status(
            scope(), self.centro.id, StoreStatusRequest(is_open=False)
        )

        self.assertFalse(self.centro.is_open)
        self.assertTrue(self.aldeota.is_open)

    def test_store_status_does_not_touch_the_order_types(self):
        # E botao de acao rapida: o corpo de um campo so nao pode arrastar
        # junto o que estava aberto em outra tela.
        response = self.service().set_store_status(
            scope(), self.centro.id, StoreStatusRequest(is_open=False)
        )

        self.assertFalse(response.is_open)
        self.assertTrue(self.centro.accepts_delivery)
        self.assertTrue(self.centro.accepts_pickup)

    def test_order_types_are_edited_one_at_a_time(self):
        self.service().set_branch_order_types(
            scope(), self.centro.id, AdminBranchOrderTypesRequest(accepts_delivery=False)
        )

        self.assertFalse(self.centro.accepts_delivery)
        self.assertTrue(self.centro.accepts_pickup)
        self.assertTrue(self.aldeota.accepts_delivery)

    def test_a_branch_outside_the_scope_is_404(self):
        # 404 e nao 403: um 403 confirmaria que a filial existe.
        with self.assertRaises(HTTPException) as raised:
            self.service().set_store_status(
                scope(branch_id=self.centro.id),
                self.aldeota.id,
                StoreStatusRequest(is_open=False),
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertTrue(self.aldeota.is_open)

    def test_is_open_now_combines_the_switch_with_the_schedule(self):
        """Aberta pela chave e fechada pelo relogio.

        E o caso que faz o lojista ligar reclamando que nao entra pedido: ele
        ve a loja "aberta" no painel e a agenda de hoje ja fechou. Sem os dois
        campos lado a lado, a tela nao tem como dizer isso.
        """
        sem_faixa = self.service()
        linha = sem_faixa.list_branch_operation(scope(), self.centro.id)[0]

        self.assertTrue(linha.is_open)
        self.assertFalse(linha.is_open_now)

    def test_the_listing_only_narrows(self):
        preso = scope(branch_id=self.centro.id)

        so_a_dele = self.service().list_branch_operation(preso, None)
        self.assertEqual([item.branch_id for item in so_a_dele], [self.centro.id])

        with self.assertRaises(HTTPException) as raised:
            self.service().list_branch_operation(preso, self.aldeota.id)
        self.assertEqual(raised.exception.status_code, 404)

    def test_the_owner_sees_every_branch(self):
        linhas = self.service().list_branch_operation(scope(), None)
        self.assertEqual(len(linhas), 2)

    def test_an_override_wins_and_the_rest_keeps_inheriting(self):
        self.service().update_branch_settings(
            scope(),
            self.centro.id,
            AdminBranchSettingsUpdate(min_order_value=Decimal("40.00")),
        )
        linha = self.service().list_branch_operation(scope(), self.centro.id)[0]

        self.assertEqual(linha.overrides.min_order_value, 40.00)
        self.assertEqual(linha.effective.min_order_value, 40.00)
        # Nao sobrescrito: continua vindo do restaurante, e mudar o padrao
        # continua chegando nesta filial.
        self.assertIsNone(linha.overrides.service_fee_amount)
        self.assertEqual(linha.effective.service_fee_amount, 0.99)

    def test_an_explicit_null_goes_back_to_inheriting(self):
        """O terceiro estado do PATCH, e o motivo de `exclude_unset`.

        Sem ele nao haveria como desfazer uma divergencia: a filial ficaria
        com a copia congelada para sempre e a proxima edicao do padrao nao
        chegaria nela.
        """
        self.centro.min_order_value = Decimal("40.00")

        self.service().update_branch_settings(
            scope(), self.centro.id, AdminBranchSettingsUpdate(min_order_value=None)
        )

        self.assertIsNone(self.centro.min_order_value)
        linha = self.service().list_branch_operation(scope(), self.centro.id)[0]
        self.assertEqual(linha.effective.min_order_value, 20.00)

    def test_a_field_absent_from_the_body_is_not_touched(self):
        self.centro.service_fee_amount = Decimal("2.00")

        self.service().update_branch_settings(
            scope(), self.centro.id, AdminBranchSettingsUpdate(min_order_value=Decimal("40.00"))
        )

        self.assertEqual(self.centro.service_fee_amount, Decimal("2.00"))

    def test_money_is_stored_with_two_decimals(self):
        self.service().update_branch_settings(
            scope(), self.centro.id, AdminBranchSettingsUpdate(service_fee_amount=Decimal("1.239"))
        )

        self.assertEqual(self.centro.service_fee_amount, Decimal("1.24"))

    def test_the_delivery_time_range_is_validated_against_the_branch(self):
        # 20 e valido sozinho; impossivel para um minimo de 40 ja gravado NA
        # FILIAL. A mescla e com a filial, nao com o padrao do restaurante.
        self.centro.estimated_delivery_time_min = 40

        with self.assertRaises(HTTPException) as raised:
            self.service().update_branch_settings(
                scope(),
                self.centro.id,
                AdminBranchSettingsUpdate(estimated_delivery_time_max=20),
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIsNone(self.centro.estimated_delivery_time_max)

    def test_a_branch_with_no_settings_row_still_answers(self):
        """Restaurante que nunca passou pelo painel nao tem o que herdar.

        Nao e erro: a linha de `restaurant_settings` sempre foi opcional, e o
        restaurante que nunca a criou continua vendendo.
        """
        sem_padrao = build_service(
            settings_repository=FakeSettingsRepository(settings=None),
            branch_repository=self.branches,
        )

        linha = sem_padrao.list_branch_operation(scope(), self.centro.id)[0]

        self.assertTrue(linha.is_open)
        self.assertEqual(linha.effective.min_order_value, 0.0)
        self.assertIsNone(linha.effective.default_delivery_fee)
