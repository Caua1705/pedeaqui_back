"""Cupom restrito a horario do dia: "vale das 15h as 18h".

O que estes testes protegem:

1. **A hora e a da OPERACAO** (America/Fortaleza), nunca UTC. `valid_from`
   e `valid_until` sao instantes; "das 15h as 18h" e hora local do
   restaurante, e o backend inteiro roda em UTC.
2. **A faixa que vira a noite pertence ao dia em que comeca** (22h as 2h),
   a mesma regra de `branch_business_hours` (armadilha 10).
3. **Inicio inclusivo, fim exclusivo**: "ate as 18h" acaba as 18:00:00.
4. **O card tem estado proprio**, `outside_hours`, com a faixa escrita:
   o cliente nao resolve agora, mas resolve as 15h — e o card precisa dizer.
   E o pedido recusa fora da faixa, sem tolerancia: a regra sem tolerancia
   e a que o lojista escreveu, e o app mostra a faixa antes do clique.
"""

import unittest
from datetime import datetime, time, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import ValidationError

from src.core.constants import PLATFORM_TIMEZONE
from src.schemas.coupon_schema import CustomerCouponState
from src.services.coupon_service import REASON_TO_STATE, CouponService
from src.services.coupon_window import dentro_do_horario
from tests.test_coupons import NOW, PUBLICO, FakeCouponRepository, make_coupon, make_customer
from tests.test_cupom_forma_de_pagamento import _campanha


FORTALEZA = ZoneInfo(PLATFORM_TIMEZONE)


def _em_fortaleza(hora: int, minuto: int = 0) -> datetime:
    """Um instante do dia de NOW, na hora local da operacao, como UTC."""
    local = NOW.astimezone(FORTALEZA).replace(hour=hora, minute=minuto, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


class TestOSchema(unittest.TestCase):
    def test_a_faixa_inteira_passa(self):
        campanha = _campanha(valid_hours_from=time(15, 0), valid_hours_until=time(18, 0))
        self.assertEqual((campanha.valid_hours_from, campanha.valid_hours_until), (time(15, 0), time(18, 0)))

    def test_ausentes_e_o_dia_inteiro(self):
        self.assertIsNone(_campanha().valid_hours_from)

    def test_pela_metade_e_recusada(self):
        with self.assertRaises(ValidationError):
            _campanha(valid_hours_from=time(15, 0))

    def test_inicio_igual_ao_fim_e_recusado(self):
        """Uma faixa de zero minutos nao e "o dia inteiro" nem "nunca": e um
        cupom que ninguem sabe quando vale."""
        with self.assertRaises(ValidationError):
            _campanha(valid_hours_from=time(15, 0), valid_hours_until=time(15, 0))

    def test_a_faixa_que_vira_a_noite_passa(self):
        _campanha(valid_hours_from=time(22, 0), valid_hours_until=time(2, 0))


class TestOPredicado(unittest.TestCase):
    def test_sem_faixa_e_sempre(self):
        self.assertTrue(dentro_do_horario(None, None, time(3, 0)))

    def test_faixa_do_mesmo_dia_inicio_inclusivo_fim_exclusivo(self):
        for hora, esperado in ((time(14, 59), False), (time(15, 0), True), (time(17, 59), True), (time(18, 0), False)):
            with self.subTest(hora=hora):
                self.assertEqual(dentro_do_horario(time(15, 0), time(18, 0), hora), esperado)

    def test_faixa_que_vira_a_noite(self):
        for hora, esperado in ((time(21, 59), False), (time(22, 0), True), (time(23, 30), True), (time(1, 59), True), (time(2, 0), False), (time(12, 0), False)):
            with self.subTest(hora=hora):
                self.assertEqual(dentro_do_horario(time(22, 0), time(2, 0), hora), esperado)


class _Base(unittest.TestCase):
    def setUp(self):
        self.coupon = make_coupon(valid_hours_from=time(15, 0), valid_hours_until=time(18, 0))
        self.customer = make_customer()
        self.service = CouponService(SimpleNamespace())
        self.repository = FakeCouponRepository(self.coupon)
        self.service.repository = self.repository
        self.service.restaurant_service = SimpleNamespace(
            get_active_restaurant=lambda slug: SimpleNamespace(id=self.coupon.restaurant_id)
        )

    def evaluate(self, now):
        return self.service.evaluate(
            self.coupon,
            restaurant_id=self.coupon.restaurant_id,
            subtotal=Decimal("100"),
            delivery_fee=Decimal("0"),
            customer=self.customer,
            audience=PUBLICO,
            now=now,
        )


class TestEvaluate(_Base):
    def test_dentro_da_faixa_local_cabe(self):
        self.assertTrue(self.evaluate(_em_fortaleza(17, 30)).valid)

    def test_fora_da_faixa_local_nao_cabe(self):
        avaliacao = self.evaluate(_em_fortaleza(19, 0))

        self.assertFalse(avaliacao.valid)
        self.assertEqual(avaliacao.reason, "outside_hours")

    def test_a_hora_e_a_da_operacao_e_nao_utc(self):
        """17h em Fortaleza sao 20h UTC. Lido em UTC, o cupom das 15h as 18h
        estaria FORA as 17h locais — e o lojista veria a campanha da tarde
        valendo de manha."""
        vinte_utc = _em_fortaleza(17, 0)
        self.assertEqual(vinte_utc.astimezone(timezone.utc).hour, 20)

        self.assertTrue(self.evaluate(vinte_utc).valid)

    def test_o_motivo_vira_estado_de_card(self):
        self.assertEqual(REASON_TO_STATE["outside_hours"], CustomerCouponState.OUTSIDE_HOURS)


class TestACardEOPedido(_Base):
    def _list(self, now):
        self.service.clock = lambda: now
        return self.service.list_for_customer(
            "r", subtotal=Decimal("100"), delivery_fee=Decimal("0"), order_type="pickup", customer=self.customer
        )

    def test_fora_da_faixa_o_card_vem_com_o_estado_e_a_faixa(self):
        card = self._list(_em_fortaleza(19, 0)).coupons[0]

        self.assertEqual(card.state, "outside_hours")
        self.assertEqual(card.valid_hours_from, time(15, 0))
        self.assertEqual(card.valid_hours_until, time(18, 0))
        self.assertEqual(card.discount_amount, Decimal("0.00"))
        self.assertFalse(card.auto_apply)

    def test_dentro_da_faixa_e_applicable_com_a_faixa_escrita(self):
        card = self._list(_em_fortaleza(16, 0)).coupons[0]

        self.assertEqual(card.state, "applicable")
        self.assertEqual(card.valid_hours_from, time(15, 0))

    def test_cupom_sem_faixa_sai_com_nulos(self):
        self.coupon.valid_hours_from = None
        self.coupon.valid_hours_until = None

        card = self._list(_em_fortaleza(3, 0)).coupons[0]

        self.assertEqual(card.state, "applicable")
        self.assertIsNone(card.valid_hours_from)

    def test_o_pedido_recusa_fora_da_faixa(self):
        self.service.clock = lambda: _em_fortaleza(18, 0)

        with self.assertRaises(HTTPException) as raised:
            self.service.lock_and_validate_for_order(
                restaurant_id=self.coupon.restaurant_id,
                coupon_id=self.coupon.id,
                coupon_code=None,
                subtotal=Decimal("100"),
                delivery_fee=Decimal("0"),
                customer=self.customer,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "outside_hours")
