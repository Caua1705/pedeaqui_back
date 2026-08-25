"""A fila de estorno pendente — contra o Postgres.

Contra banco de verdade porque a fila **é** uma consulta: não existe coluna
de "estorno pendente", e a decisão de não criar uma é o que este arquivo
protege. Dublar o `SELECT` seria testar o dublê exatamente onde o desenho
mora.

O que se prova aqui é que o conjunto é **exato** nas duas direções:

- entra quem tem dinheiro (ou cobrança viva) preso: pedido cancelado ou
  recusado, com pagamento online e `payment_status` ainda em pending,
  in_review ou paid;
- **sai sozinho** assim que o estorno é aplicado, sem ninguém marcar nada —
  `refunded` e `failed` não estão na lista, e é isso que substitui a coluna
  de fila;
- e não entra o pedido **concluído**, que também é terminal e é o único
  terminal em que houve venda. Um filtro por `TERMINAL_ORDER_STATUSES`
  devolveria o dinheiro de todo pedido entregue — foi o defeito que os
  testes rápidos pegaram, e este é o lado dele que vive no `WHERE`.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.repositories.order_repository import OrderRepository
from tests.fabricas_db import criar_filial, criar_pedido, criar_restaurante


pytestmark = pytest.mark.db

AGORA = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
DESDE = AGORA - timedelta(days=90)


def pedido_online(db, restaurante, filial, *, status, payment_status, criado_em=None):
    """Pedido com cobrança online de verdade no gateway.

    As três colunas de pagamento não passam pela fábrica porque quase nenhum
    teste precisa delas — aqui elas são o assunto.
    """
    pedido = criar_pedido(
        db,
        restaurante,
        filial,
        status=status,
        payment_status=payment_status,
        created_at=criado_em or AGORA - timedelta(hours=1),
    )
    pedido.payment_flow = "online"
    pedido.payment_method = "pix"
    pedido.payment_provider = "mercadopago"
    pedido.provider_payment_id = f"mp-{uuid.uuid4().hex[:12]}"
    db.flush()
    return pedido


def ids_pendentes(db):
    return {
        pedido.id for pedido in OrderRepository(db).list_orders_awaiting_refund(since=DESDE)
    }


class TestQuemEntraNaFila:
    def test_pedido_cancelado_e_pago_entra(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_online(db, restaurante, filial, status="cancelled", payment_status="paid")

        assert pedido.id in ids_pendentes(db)

    def test_pedido_recusado_e_pago_entra(self, db):
        # A outra ponta da mesma corrida: o lojista recusou e o pagamento
        # entrou depois. O dinheiro é o mesmo.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_online(db, restaurante, filial, status="rejected", payment_status="paid")

        assert pedido.id in ids_pendentes(db)

    def test_pix_aberto_de_pedido_cancelado_entra(self, db):
        # Não há dinheiro preso, mas há uma cobrança que o cliente ainda
        # consegue pagar no app do banco — de um pedido que ninguém vai
        # produzir.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_online(
            db, restaurante, filial, status="cancelled", payment_status="pending"
        )

        assert pedido.id in ids_pendentes(db)

    def test_cartao_em_analise_de_pedido_cancelado_entra(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_online(
            db, restaurante, filial, status="cancelled", payment_status="in_review"
        )

        assert pedido.id in ids_pendentes(db)


class TestQuemFicaDeFora:
    def test_pedido_concluido_e_pago_nao_entra(self, db):
        # `completed` também é terminal, e é o ÚNICO terminal em que houve
        # venda. Se ele entrasse, a varredura devolveria o dinheiro de todo
        # pedido entregue — todo dia, sozinha.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_online(db, restaurante, filial, status="completed", payment_status="paid")

        assert pedido.id not in ids_pendentes(db)

    def test_pedido_vivo_nao_entra(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_online(db, restaurante, filial, status="preparing", payment_status="paid")

        assert pedido.id not in ids_pendentes(db)

    def test_pedido_cancelado_pago_na_entrega_nao_entra(self, db):
        # Nunca houve cobrança: não há o que devolver nem o que cancelar.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = criar_pedido(
            db, restaurante, filial, status="cancelled", payment_status="on_delivery"
        )

        assert pedido.id not in ids_pendentes(db)

    def test_pedido_online_sem_cobranca_criada_nao_entra(self, db):
        # O cliente fechou o checkout antes de clicar em pagar: não há id de
        # cobrança para consultar no gateway.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = criar_pedido(
            db, restaurante, filial, status="cancelled", payment_status="pending"
        )
        pedido.payment_flow = "online"
        db.flush()

        assert pedido.id not in ids_pendentes(db)

    def test_pedido_fora_da_janela_nao_entra(self, db):
        # Passado o prazo de estorno do gateway, retentar não devolve
        # dinheiro nenhum e o conserto é humano.
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_online(
            db,
            restaurante,
            filial,
            status="cancelled",
            payment_status="paid",
            criado_em=DESDE - timedelta(days=1),
        )

        assert pedido.id not in ids_pendentes(db)


class TestSaidaDaFila:
    """O que substitui a coluna de fila: o pedido sai sozinho."""

    def test_estornado_sai_da_fila_sem_ninguem_marcar_nada(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_online(db, restaurante, filial, status="cancelled", payment_status="paid")
        assert pedido.id in ids_pendentes(db)

        pedido.payment_status = "refunded"
        db.flush()

        assert pedido.id not in ids_pendentes(db)

    def test_cobranca_cancelada_sai_da_fila(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = pedido_online(
            db, restaurante, filial, status="cancelled", payment_status="pending"
        )
        assert pedido.id in ids_pendentes(db)

        # `failed` é o que o cancelamento da cobrança grava — o `cancelled`
        # do Mercado Pago já traduz para `failed` aqui.
        pedido.payment_status = "failed"
        db.flush()

        assert pedido.id not in ids_pendentes(db)


def test_a_fila_atravessa_restaurantes(db):
    """Sem filtro por restaurante, de propósito: quem varre é manutenção, não
    um tenant. Um filtro aqui deixaria a fila de cada loja invisível."""
    primeiro = criar_restaurante(db, nome="Junior da Picanha")
    segundo = criar_restaurante(db, nome="Varjota Burguer")
    pedido_a = pedido_online(
        db, primeiro, criar_filial(db, primeiro), status="cancelled", payment_status="paid"
    )
    pedido_b = pedido_online(
        db, segundo, criar_filial(db, segundo), status="rejected", payment_status="paid"
    )

    pendentes = ids_pendentes(db)

    assert {pedido_a.id, pedido_b.id} <= pendentes
