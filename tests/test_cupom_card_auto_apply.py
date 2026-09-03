"""`visibility` e `auto_apply` no card do cliente.

O front pediu os dois: a etiqueta "para todos" precisa saber que o cupom e
publico, e a tela do checkout precisa saber QUAL cupom vai entrar sozinho —
sem refazer a conta do lado de la.

**`auto_apply` e calculado pela MESMA escolha do checkout.** Entre dois
cupons automaticos que cabem, `auto_apply_for_order` escolhe o de maior
desconto (desempate por `sort_order` e `id`). Se a listagem escolhesse por
conta propria, o card diria "este entra sozinho" e o checkout aplicaria
outro — decisao de dinheiro tomada duas vezes, em dois lugares.
"""

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace

from src.models.coupon_model import COUPON_VISIBILITY_PRIVATE, COUPON_VISIBILITY_SEGMENT
from src.services.coupon_service import CouponService
from tests.test_coupons import NOW, FakeCouponRepository, make_coupon, make_customer


class FakeRepositorioComVarios(FakeCouponRepository):
    """O dublê da suite de cupons segura UM cupom; a escolha automatica
    precisa de varios na mesma vitrine."""

    def __init__(self, coupons):
        super().__init__(coupons[0])
        self.coupons = list(coupons)

    def list_in_window(self, restaurant_id, now=None):
        return [c for c in self.coupons if c.restaurant_id == restaurant_id]

    def get_by_id_and_restaurant(self, coupon_id, restaurant_id, for_update=False, agora=None):
        for coupon in self.coupons:
            if coupon.id == coupon_id and coupon.restaurant_id == restaurant_id:
                return coupon
        return None

    def lock_coupon(self, restaurant_id, coupon_id=None, coupon_code=None, agora=None):
        self.lock_calls += 1
        return self.get_by_id_and_restaurant(coupon_id, restaurant_id)


class _Base(unittest.TestCase):
    def setUp(self):
        self.restaurant_id = uuid.uuid4()
        self.customer = make_customer()
        self.automatico_10 = make_coupon(
            restaurant_id=self.restaurant_id, code=None, title="Auto 10", discount_value=Decimal("10")
        )
        self.automatico_15 = make_coupon(
            restaurant_id=self.restaurant_id, code=None, title="Auto 15", discount_value=Decimal("15")
        )
        self.com_codigo = make_coupon(restaurant_id=self.restaurant_id, code="PROMO5", discount_value=Decimal("5"))
        self.service = CouponService(SimpleNamespace())
        self.repository = FakeRepositorioComVarios([self.automatico_10, self.automatico_15, self.com_codigo])
        self.service.repository = self.repository
        self.service.clock = lambda: NOW
        self.service.restaurant_service = SimpleNamespace(
            get_active_restaurant=lambda slug: SimpleNamespace(id=self.restaurant_id)
        )

    def _list(self, **overrides):
        return self.service.list_for_customer(
            "restaurante",
            subtotal=overrides.pop("subtotal", Decimal("100")),
            delivery_fee=overrides.pop("delivery_fee", Decimal("0")),
            order_type=overrides.pop("order_type", "pickup"),
            customer=overrides.pop("customer", self.customer),
        )

    def _por_titulo(self, **overrides):
        return {card.title: card for card in self._list(**overrides).coupons}


class TestVisibility(_Base):
    def test_o_card_diz_a_visibilidade(self):
        self.com_codigo.visibility = COUPON_VISIBILITY_SEGMENT
        self.com_codigo.target_segment = "fiel"
        self.automatico_10.visibility = COUPON_VISIBILITY_PRIVATE
        self.repository.claims.add(self.automatico_10.id)

        cards = self._por_titulo()

        self.assertEqual(cards["Promocao"].visibility, "segment")
        self.assertEqual(cards["Auto 10"].visibility, "private")
        self.assertEqual(cards["Auto 15"].visibility, "public")


class TestAutoApply(_Base):
    def test_so_o_maior_automatico_que_cabe_e_marcado(self):
        cards = self._por_titulo()

        self.assertTrue(cards["Auto 15"].auto_apply)
        self.assertFalse(cards["Auto 10"].auto_apply)
        self.assertFalse(cards["Promocao"].auto_apply)

    def test_e_e_o_mesmo_que_o_checkout_escolhe(self):
        """A prova de que a decisao mora num lugar so."""
        cards = self._por_titulo()
        marcado = next(card for card in cards.values() if card.auto_apply)

        escolhido, _ = self.service.auto_apply_for_order(
            restaurant_id=self.restaurant_id,
            subtotal=Decimal("100"),
            delivery_fee=Decimal("0"),
            customer=self.customer,
        )

        self.assertEqual(marcado.id, escolhido.id)

    def test_o_desempate_e_o_mesmo_nos_dois_lados(self):
        self.automatico_15.discount_value = Decimal("10")
        self.automatico_15.sort_order = 0
        self.automatico_10.sort_order = 1

        cards = self._por_titulo()
        escolhido, _ = self.service.auto_apply_for_order(
            restaurant_id=self.restaurant_id,
            subtotal=Decimal("100"),
            delivery_fee=Decimal("0"),
            customer=self.customer,
        )

        marcados = [card.title for card in cards.values() if card.auto_apply]
        self.assertEqual(len(marcados), 1)
        self.assertEqual(marcados[0], next(c.title for c in self.repository.coupons if c.id == escolhido.id))

    def test_automatico_que_nao_cabe_nao_e_marcado(self):
        """`missing_amount` vem no card, mas nao entra sozinho: o checkout
        tambem nao o aplicaria."""
        self.automatico_15.min_order_value = Decimal("200")

        cards = self._por_titulo()

        self.assertEqual(cards["Auto 15"].state, "missing_amount")
        self.assertFalse(cards["Auto 15"].auto_apply)
        self.assertTrue(cards["Auto 10"].auto_apply)

    def test_convidado_nao_tem_auto_apply(self):
        """O checkout nao aplica cupom automatico a convidado (nao ha onde
        registrar o uso); o card nao pode prometer o contrario."""
        cards = self._por_titulo(customer=None)

        self.assertFalse(any(card.auto_apply for card in cards.values()))

    def test_sem_sacola_nada_e_marcado(self):
        """A tela do Clube nao tem sacola: nao ha desconto para comparar, e
        "vai entrar sozinho" so e verdade no checkout."""
        cards = self._por_titulo(subtotal=None)

        self.assertFalse(any(card.auto_apply for card in cards.values()))
