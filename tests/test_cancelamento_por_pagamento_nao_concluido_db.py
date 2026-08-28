"""O pedido órfão de uma cobrança recusada — contra o Postgres.

Contra banco de verdade pelo mesmo motivo da varredura irmã
(`test_estorno_varredura_db.py`): a fila **é** uma consulta, não existe
coluna de "cancelar depois", e dublar o `SELECT` seria testar o dublê
exatamente onde o desenho mora.

Duas metades, e a segunda é a que é dinheiro:

- o conjunto é **exato** nas duas direções — entra só o pedido `pending` +
  `online` + `failed` que passou da carência, e sai sozinho assim que é
  cancelado;
- o cancelamento **devolve o cupom e o cashback**. É o que a passagem pelo
  `OrderStatusChangeService` compra, e é o que um cancelamento com código
  próprio perderia em silêncio.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scripts.cancela_pedidos_sem_pagamento import _cancel_one
from src.models.cashback_transaction_model import CashbackTransaction
from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.models.coupon_redemption_model import CouponRedemption
from src.models.order_model import Order
from src.repositories.order_repository import OrderRepository
from src.utils.security import hash_tracking_token
from tests.fabricas_db import criar_cliente, criar_filial, criar_restaurante


pytestmark = pytest.mark.db

AGORA = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
DESDE = AGORA - timedelta(days=90)
# A carência de 30 min do script, aplicada ao relógio fixo dos testes.
PASSOU_DA_CARENCIA = AGORA - timedelta(minutes=30)


def pedido_recusado(
    db,
    restaurante,
    filial,
    *,
    cliente=None,
    status="pending",
    payment_status="failed",
    payment_flow="online",
    ultima_tentativa=None,
    criado_em=None,
):
    """Pedido online cuja cobrança já foi tentada no gateway.

    **Montado à mão, e não por `criar_pedido` + atribuições**, e é a única
    forma que funciona: `orders` tem o gatilho `trg_orders_updated_at`
    (BEFORE UPDATE) reescrevendo `updated_at` com `now()` a cada UPDATE — ele
    sobrepõe o valor até de um `UPDATE` cru. Envelhecer o pedido só é
    possível no próprio INSERT, e a fábrica já dá o `flush` antes de
    devolver. Com atribuição depois, a carência não é testável: todo pedido
    nasce recém-tocado e a consulta volta vazia sempre — inclusive nos testes
    de `TestQuemFicaDeFora`, que passariam por acidente.
    """
    pedido = Order(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        customer_id=cliente.id if cliente else None,
        tracking_token_hash=hash_tracking_token(f"token-{uuid.uuid4().hex}"),
        customer_name_snapshot="Cliente de Teste",
        customer_phone_snapshot="85999999999",
        order_type="delivery",
        status=status,
        payment_status=payment_status,
        payment_flow=payment_flow,
        payment_method="credit_card",
        payment_provider="mercadopago",
        provider_payment_id=f"mp-{uuid.uuid4().hex[:12]}",
        total=Decimal("50.00"),
        created_at=criado_em or AGORA - timedelta(hours=3),
        updated_at=ultima_tentativa or AGORA - timedelta(hours=2),
    )
    db.add(pedido)
    db.flush()
    return pedido


def ids_abandonados(db):
    return {
        pedido.id
        for pedido in OrderRepository(db).list_orders_abandoned_after_payment_failure(
            older_than=PASSOU_DA_CARENCIA, since=DESDE
        )
    }


class TestQuemEntraNaVarredura:
    def test_pedido_pendente_com_cobranca_recusada_entra(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(db, restaurante, filial)

        assert pedido.id in ids_abandonados(db)


class TestQuemFicaDeFora:
    def test_dentro_da_carencia_nao_entra(self, db):
        # O cliente ainda está pegando o segundo cartão na carteira.
        # `cancelled` é terminal: cancelar cedo demais o obriga a refazer o
        # carrinho inteiro, e não há volta.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(
            db, restaurante, filial, ultima_tentativa=AGORA - timedelta(minutes=5)
        )

        assert pedido.id not in ids_abandonados(db)

    def test_pix_ainda_pagavel_nao_entra(self, db):
        # `payment_status='pending'` é a cobrança que o cliente ainda
        # consegue pagar no app do banco. Ficou de fora de propósito: é o
        # "já que estou aqui" que cancelaria pix bom.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(db, restaurante, filial, payment_status="pending")

        assert pedido.id not in ids_abandonados(db)

    def test_cartao_em_analise_nao_entra(self, db):
        # `in_review` é o antifraude segurando a cobrança, e ele pode aprovar
        # em até 48h úteis. Um `!= "paid"` no lugar do `== "failed"` traria
        # este pedido junto — e cancelá-lo é cancelar dinheiro que está
        # entrando.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(db, restaurante, filial, payment_status="in_review")

        assert pedido.id not in ids_abandonados(db)

    def test_pedido_pago_nao_entra(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(db, restaurante, filial, payment_status="paid")

        assert pedido.id not in ids_abandonados(db)

    def test_pago_na_entrega_nao_entra(self, db):
        # Nunca houve cobrança a falhar.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(
            db, restaurante, filial, payment_flow="delivery", payment_status="on_delivery"
        )

        assert pedido.id not in ids_abandonados(db)

    def test_pedido_que_o_lojista_ja_moveu_nao_entra(self, db):
        # Hoje a trava do aceite impede este estado, e é justamente por isso
        # que a condição é `status == "pending"` e não
        # `not_in(TERMINAL_ORDER_STATUSES)`: pedido que alguém moveu é
        # assunto de quem o moveu.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(db, restaurante, filial, status="accepted")

        assert pedido.id not in ids_abandonados(db)

    def test_pedido_ja_cancelado_nao_entra(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(db, restaurante, filial, status="cancelled")

        assert pedido.id not in ids_abandonados(db)

    def test_fora_da_janela_de_varredura_nao_entra(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(
            db, restaurante, filial, criado_em=DESDE - timedelta(days=1)
        )

        assert pedido.id not in ids_abandonados(db)


class TestOEfeitoDoCancelamento:
    """O que a passagem pelo `OrderStatusChangeService` compra."""

    def test_o_pedido_sai_da_fila_sozinho(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(db, restaurante, filial)

        assert _cancel_one(db, pedido.id, restaurante.id, pedido.order_number) is True

        db.refresh(pedido)
        assert pedido.status == "cancelled"
        assert pedido.id not in ids_abandonados(db)

    def test_o_cupom_volta_para_o_cliente(self, db):
        # Sem isto, o cliente cujo cartão foi recusado perde o cupom num
        # pedido que não existe — e não consegue usá-lo de novo, porque a
        # redenção `applied` conta contra `usage_limit_per_customer`.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        cliente = criar_cliente(db)
        pedido = pedido_recusado(db, restaurante, filial, cliente=cliente)
        resgate = _resgatar_cupom(db, restaurante, cliente, pedido)

        _cancel_one(db, pedido.id, restaurante.id, pedido.order_number)

        db.refresh(resgate)
        assert resgate.status != "applied"

    def test_o_cashback_volta_para_o_saldo(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        cliente = criar_cliente(db)
        pedido = pedido_recusado(db, restaurante, filial, cliente=cliente)
        pedido.cashback_redeemed_amount = Decimal("7.50")
        db.add(
            CashbackTransaction(
                customer_id=cliente.id,
                restaurant_id=restaurante.id,
                order_id=pedido.id,
                type="redeemed",
                amount=Decimal("-7.50"),
                status="available",
                idempotency_key=f"cashback:redeem:{pedido.id}",
            )
        )
        db.flush()

        _cancel_one(db, pedido.id, restaurante.id, pedido.order_number)

        devolucao = (
            db.query(CashbackTransaction)
            .filter_by(idempotency_key=f"cashback:refund:{pedido.id}")
            .one_or_none()
        )
        assert devolucao is not None
        assert devolucao.amount == Decimal("7.50")

    def test_o_historico_registra_quem_cancelou_e_por_que(self, db):
        # O lojista e o suporte leem esta linha para separar "o restaurante
        # cancelou" de "o pagamento nunca foi concluído".
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(db, restaurante, filial)

        _cancel_one(db, pedido.id, restaurante.id, pedido.order_number)

        detalhe = OrderRepository(db).get_order_detail(pedido.id, restaurante.id)
        cancelamento = [linha for linha in detalhe.status_history if linha.status == "cancelled"]
        assert len(cancelamento) == 1
        assert cancelamento[0].changed_by == "sistema"
        assert "pagamento" in (cancelamento[0].note or "").lower()

    def test_pedido_pago_entre_a_leitura_e_a_escrita_e_poupado(self, db):
        # A corrida real: o cliente volta e paga depois do SELECT da lista.
        # `ensure_payment_allows_order_status` NÃO barraria isso — ela libera
        # `cancelled` em qualquer estado de pagamento, de propósito. Quem
        # barra é a releitura dentro de `_cancel_one`.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_recusado(db, restaurante, filial)
        numero = pedido.order_number

        pedido.payment_status = "paid"
        db.flush()

        assert _cancel_one(db, pedido.id, restaurante.id, numero) is False

        db.refresh(pedido)
        assert pedido.status == "pending"


def _resgatar_cupom(db, restaurante, cliente, pedido) -> CouponRedemption:
    template = CouponTemplate(
        name=f"Arte {uuid.uuid4().hex[:6]}",
        image_path="coupons/arte.png",
        discount_type="fixed",
        discount_value=Decimal("10"),
        sort_order=0,
        is_active=True,
    )
    db.add(template)
    db.flush()

    cupom = RestaurantCoupon(
        restaurant_id=restaurante.id,
        coupon_template_id=template.id,
        code=f"CUPOM{uuid.uuid4().hex[:8].upper()}",
        title="Campanha de teste",
        discount_type="fixed",
        discount_value=Decimal("10"),
        min_order_value=Decimal("0"),
        valid_from=AGORA - timedelta(days=1),
        valid_until=AGORA + timedelta(days=30),
        first_order_only=False,
        visibility="public",
        is_active=True,
    )
    db.add(cupom)
    db.flush()

    resgate = CouponRedemption(
        coupon_id=cupom.id,
        customer_id=cliente.id,
        order_id=pedido.id,
        discount_amount=Decimal("10.00"),
        status="applied",
        idempotency_key=f"order:{pedido.id}",
    )
    db.add(resgate)
    db.flush()
    return resgate
