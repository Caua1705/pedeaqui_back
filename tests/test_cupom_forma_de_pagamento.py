"""Cupom restrito a forma de pagamento: "este cupom so vale no pix".

O que estes testes protegem:

1. **A regra mora em `evaluate`**, e os quatro chamadores passam a forma de
   pagamento: listagem, preview, auto-aplicacao e a validacao do pedido.
   Sem isso um cupom "so no pix" seria aplicado a quem paga no cartao.
2. **O card tem estado proprio.** `payment_method_not_allowed` e o que o
   cliente RESOLVE (trocando a forma), entao o card aparece, com
   `allowed_payment_methods` para o app escrever "so no pix".
3. **Sem forma escolhida, o cupom cabe.** A forma e a ultima coisa que o
   cliente escolhe; a lista sem `payment_method` e o Clube e a sacola
   antes da escolha, e o card ja diz em que forma vale. A validacao que
   vale e a do pedido, que sempre tem a forma.
4. **As duas listas concordam.** O schema recusa forma fora de
   `PAYMENT_METHODS` (armadilha 15).
"""

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError

from src.schemas.coupon_schema import (
    CouponCampaignFields,
    CouponPreviewRequest,
    CouponUpdate,
    CustomerCouponState,
)
from src.schemas.order_schema import CreateOrderRequest
from src.services.coupon_service import REASON_TO_STATE, CouponService
from src.services.order_service import OrderService
from tests.test_coupons import NOW, PUBLICO, FakeCouponRepository, make_coupon, make_customer


def _campanha(**extras):
    campos = dict(
        coupon_template_id=uuid.uuid4(),
        code="PIX10",
        title="So no pix",
        discount_type="fixed",
        discount_value=Decimal("10"),
        valid_from=NOW,
    )
    campos.update(extras)
    return CouponCampaignFields(**campos)


class TestOSchema(unittest.TestCase):
    def test_aceita_uma_lista_de_formas_conhecidas(self):
        self.assertEqual(_campanha(allowed_payment_methods=["pix", "cash"]).allowed_payment_methods, ["pix", "cash"])

    def test_ausente_e_nulo_e_significa_qualquer_forma(self):
        self.assertIsNone(_campanha().allowed_payment_methods)

    def test_forma_desconhecida_e_recusada(self):
        with self.assertRaises(ValidationError):
            _campanha(allowed_payment_methods=["banana"])

    def test_lista_vazia_e_recusada(self):
        """Vazia nao e "qualquer forma": e "nenhuma", e um cupom que nao vale
        em forma nenhuma e um cupom que ninguem consegue usar. Quem quer
        qualquer forma manda `null`."""
        with self.assertRaises(ValidationError):
            _campanha(allowed_payment_methods=[])

    def test_repetida_e_colapsada(self):
        self.assertEqual(_campanha(allowed_payment_methods=["pix", "pix"]).allowed_payment_methods, ["pix"])

    def test_o_patch_aceita_nulo_para_tirar_a_restricao(self):
        self.assertIsNone(CouponUpdate(allowed_payment_methods=None).allowed_payment_methods)
        self.assertEqual(CouponUpdate(allowed_payment_methods=["pix"]).allowed_payment_methods, ["pix"])

    def test_o_preview_aceita_a_forma(self):
        request = CouponPreviewRequest(
            coupon_code="PIX10", subtotal=Decimal("50"), order_type="delivery", payment_method="credit_card"
        )
        self.assertEqual(request.payment_method, "credit_card")


class _Base(unittest.TestCase):
    def setUp(self):
        self.coupon = make_coupon(allowed_payment_methods=["pix"])
        self.customer = make_customer()
        self.service = CouponService(SimpleNamespace())
        self.repository = FakeCouponRepository(self.coupon)
        self.service.repository = self.repository
        self.service.clock = lambda: NOW
        self.service.restaurant_service = SimpleNamespace(
            get_active_restaurant=lambda slug: SimpleNamespace(id=self.coupon.restaurant_id)
        )

    def evaluate(self, **overrides):
        return self.service.evaluate(
            self.coupon,
            restaurant_id=self.coupon.restaurant_id,
            subtotal=Decimal("100"),
            delivery_fee=Decimal("8"),
            customer=self.customer,
            audience=PUBLICO,
            now=NOW,
            **overrides,
        )

    def _list(self, **overrides):
        return self.service.list_for_customer(
            "r",
            subtotal=overrides.pop("subtotal", Decimal("100")),
            delivery_fee=Decimal("0"),
            order_type="pickup",
            customer=overrides.pop("customer", self.customer),
            payment_method=overrides.pop("payment_method", None),
        )


class TestEvaluate(_Base):
    def test_na_forma_permitida_cabe(self):
        self.assertTrue(self.evaluate(payment_method="pix").valid)

    def test_em_outra_forma_nao_cabe_com_motivo_proprio(self):
        avaliacao = self.evaluate(payment_method="credit_card")

        self.assertFalse(avaliacao.valid)
        self.assertEqual(avaliacao.reason, "payment_method_not_allowed")
        self.assertEqual(avaliacao.discount, Decimal("0"))

    def test_sem_forma_escolhida_cabe(self):
        """A forma e a ultima coisa que o cliente escolhe; quem barra e o
        pedido, que sempre a tem."""
        self.assertTrue(self.evaluate(payment_method=None).valid)

    def test_cupom_sem_restricao_ignora_a_forma(self):
        self.coupon.allowed_payment_methods = None
        self.assertTrue(self.evaluate(payment_method="credit_card").valid)

    def test_o_motivo_vira_estado_de_card(self):
        self.assertEqual(REASON_TO_STATE["payment_method_not_allowed"], CustomerCouponState.PAYMENT_METHOD_NOT_ALLOWED)


class TestAListagem(_Base):
    def test_com_a_forma_errada_o_card_vem_com_o_estado_e_as_formas(self):
        card = self._list(payment_method="credit_card").coupons[0]

        self.assertEqual(card.state, "payment_method_not_allowed")
        self.assertEqual(card.allowed_payment_methods, ["pix"])
        self.assertEqual(card.discount_amount, Decimal("0.00"))
        self.assertFalse(card.auto_apply)

    def test_com_a_forma_certa_e_applicable(self):
        card = self._list(payment_method="pix").coupons[0]

        self.assertEqual(card.state, "applicable")
        self.assertEqual(card.allowed_payment_methods, ["pix"])

    def test_sem_forma_e_applicable_e_o_card_ja_diz_em_qual_vale(self):
        card = self._list().coupons[0]

        self.assertEqual(card.state, "applicable")
        self.assertEqual(card.allowed_payment_methods, ["pix"])

    def test_cupom_sem_restricao_sai_com_nulo(self):
        self.coupon.allowed_payment_methods = None
        self.assertIsNone(self._list().coupons[0].allowed_payment_methods)

    def test_forma_desconhecida_na_querystring_e_400(self):
        with self.assertRaises(HTTPException) as raised:
            self._list(payment_method="banana")

        self.assertEqual(raised.exception.status_code, 400)


class TestPreviewECheckout(_Base):
    def test_o_preview_explica(self):
        response = self.service.preview(
            "r",
            CouponPreviewRequest(
                coupon_code="PROMO10", subtotal=Decimal("50"), order_type="pickup", payment_method="cash"
            ),
            self.customer,
        )

        self.assertFalse(response.valid)
        self.assertEqual(response.ineligibility_reason, "payment_method_not_allowed")

    def test_o_pedido_recusa_com_400(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.lock_and_validate_for_order(
                restaurant_id=self.coupon.restaurant_id,
                coupon_id=self.coupon.id,
                coupon_code=None,
                subtotal=Decimal("100"),
                delivery_fee=Decimal("0"),
                customer=self.customer,
                payment_method="credit_card",
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "payment_method_not_allowed")

    def test_o_pedido_na_forma_certa_passa(self):
        coupon, discount = self.service.lock_and_validate_for_order(
            restaurant_id=self.coupon.restaurant_id,
            coupon_id=self.coupon.id,
            coupon_code=None,
            subtotal=Decimal("100"),
            delivery_fee=Decimal("0"),
            customer=self.customer,
            payment_method="pix",
        )

        self.assertIs(coupon, self.coupon)
        self.assertEqual(discount, Decimal("10.00"))

    def test_o_automatico_restrito_nao_entra_em_outra_forma(self):
        self.coupon.code = None

        self.assertIsNone(
            self.service.auto_apply_for_order(
                restaurant_id=self.coupon.restaurant_id,
                subtotal=Decimal("100"),
                delivery_fee=Decimal("0"),
                customer=self.customer,
                payment_method="credit_card",
            )
        )
        escolhido, _ = self.service.auto_apply_for_order(
            restaurant_id=self.coupon.restaurant_id,
            subtotal=Decimal("100"),
            delivery_fee=Decimal("0"),
            customer=self.customer,
            payment_method="pix",
        )
        self.assertIs(escolhido, self.coupon)


class TestOPedidoPassaAForma(unittest.TestCase):
    """`OrderService._resolve_coupon` e quem sabe a forma do pedido; os dois
    caminhos (escolhido e automatico) tem que repassa-la."""

    def _payload(self, **extras):
        campos = dict(
            branch_id=uuid.uuid4(),
            order_type="pickup",
            payment_method="credit_card",
            items=[{"product_id": str(uuid.uuid4()), "quantity": 1}],
        )
        campos.update(extras)
        return CreateOrderRequest(**campos)

    def test_o_escolhido_recebe_a_forma(self):
        chamadas = {}
        service = OrderService.__new__(OrderService)
        service.coupon_service = SimpleNamespace(
            lock_and_validate_for_order=lambda **kw: chamadas.update(kw) or ("cupom", Decimal("1")),
        )

        service._resolve_coupon(
            self._payload(coupon_code="PIX10"),
            restaurant_id=uuid.uuid4(),
            subtotal=Decimal("50"),
            delivery_fee=Decimal("0"),
            current_customer=None,
        )

        self.assertEqual(chamadas["payment_method"], "credit_card")

    def test_o_automatico_recebe_a_forma(self):
        chamadas = {}
        service = OrderService.__new__(OrderService)
        service.coupon_service = SimpleNamespace(
            auto_apply_for_order=lambda **kw: chamadas.update(kw) or None,
        )

        service._resolve_coupon(
            self._payload(),
            restaurant_id=uuid.uuid4(),
            subtotal=Decimal("50"),
            delivery_fee=Decimal("0"),
            current_customer=None,
        )

        self.assertEqual(chamadas["payment_method"], "credit_card")
