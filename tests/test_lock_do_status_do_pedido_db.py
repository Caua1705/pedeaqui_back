"""A corrida entre duas portas que escrevem o status do MESMO pedido.

`OrderStatusChangeService.apply` fazia ler -> validar -> escrever sem lock, e
as duas leituras concorrentes viam o mesmo status:

    pedido em `ready`
      lojista clica "concluir"           le ready -> ready->completed        OK
      entregador clica "sai p/ entrega"  le ready -> ready->out_for_delivery OK
      os dois escrevem. o ultimo ganha.

O status errado e o menor estrago. `apply` roda EFEITO COLATERAL por destino —
`completed` credita cashback, `cancelled` estorna o cupom e devolve o cashback
resgatado —, entao uma corrida `completed` x `cancelled` a partir de `ready`
roda os DOIS: o cliente leva o credito E o cupom volta, num pedido que terminou
cancelado e portanto fora do faturamento.

## Por que este arquivo tem fixture propria, e nao usa `db`

A fixture `db` abre UMA transacao numa conexao e a desfaz no fim. Isso e o
isolamento certo para quase tudo e e inutil aqui: duas sessoes dentro da mesma
transacao nao disputam lock nenhum, e uma nao enxerga o que a outra escreveu.
Um teste de corrida escrito sobre ela ficaria verde sem exercitar nada.

Aqui os dados sao COMMITADOS e apagados a mao no `finally`. E mais caro e e o
unico jeito de duas conexoes se verem.

## Os tres testes, e o que cada um prova sozinho

1. **a linha e travada de verdade** — enquanto uma sessao segura o lock, a
   outra nao consegue pegar a mesma linha. Sem isto, os outros dois passariam
   com um `SELECT` comum;
2. **a instancia em memoria e RECARREGADA** — e o teste que separa o conserto
   de um no-op. Quem chama `apply` ja carregou o pedido, entao ele esta no
   identity map da sessao; um `select()` que devolva a mesma identidade **nao
   reescreve os atributos ja carregados**, e o Postgres travaria a linha
   enquanto o Python continuava validando o status velho. Quem fecha isso e o
   `populate_existing` do repositorio;
3. **o efeito colateral do perdedor NAO roda** — que e o dinheiro. A corrida
   inteira, com o vencedor commitado e o perdedor chegando com o objeto velho
   na mao.

E o quarto, pela regra do CLAUDE.md: a MESMA chamada com o dado certo nao
levanta. Sem ele, os tres acima passariam com um `apply` que recusa tudo.
"""

import uuid

from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.models.order_model import Order
from src.models.restaurant_setting_model import RestaurantSetting
from src.repositories.order_repository import OrderRepository
from src.services.cashback_service import CashbackService
from src.services.coupon_service import CouponService
from src.services.order_status_change_service import OrderStatusChangeService
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


class Cenario:
    """Um pedido em `ready`, commitado, e as duas sessoes que o disputam."""

    def __init__(self, engine):
        self.engine = engine
        self.montagem = Session(bind=engine)
        self.restaurante = fab.criar_restaurante(self.montagem, "Corrida de status")
        self.filial = fab.criar_filial(self.montagem, self.restaurante)
        self.montagem.add(
            RestaurantSetting(
                restaurant_id=self.restaurante.id,
                min_order_value=Decimal("0.00"),
            )
        )
        self.cliente = fab.criar_cliente(self.montagem)
        self.pedido = fab.criar_pedido(
            self.montagem,
            self.restaurante,
            self.filial,
            self.cliente,
            status="ready",
            payment_status="on_delivery",
            order_type="delivery",
        )
        self.montagem.commit()
        self.order_id = self.pedido.id
        self.restaurant_id = self.restaurante.id

        # Duas conexoes independentes, como dois workers atendendo dois
        # cliques ao mesmo tempo.
        self.a = Session(bind=engine)
        self.b = Session(bind=engine)

    def fechar(self) -> None:
        for sessao in (self.a, self.b):
            sessao.rollback()
            sessao.close()
        # Na ordem das FKs. `orders` referencia cliente, filial e restaurante.
        limpeza = Session(bind=self.engine)
        try:
            limpeza.execute(
                text("DELETE FROM order_status_history WHERE order_id = :o"),
                {"o": self.order_id},
            )
            limpeza.execute(text("DELETE FROM orders WHERE id = :o"), {"o": self.order_id})
            limpeza.execute(
                text("DELETE FROM restaurant_settings WHERE restaurant_id = :r"),
                {"r": self.restaurant_id},
            )
            limpeza.execute(text("DELETE FROM customers WHERE id = :c"), {"c": self.cliente.id})
            limpeza.execute(text("DELETE FROM branches WHERE id = :b"), {"b": self.filial.id})
            limpeza.execute(
                text("DELETE FROM restaurants WHERE id = :r"), {"r": self.restaurant_id}
            )
            limpeza.commit()
        finally:
            limpeza.close()
        self.montagem.close()


@pytest.fixture
def cenario(engine_de_teste):
    montado = Cenario(engine_de_teste)
    try:
        yield montado
    finally:
        montado.fechar()


def _aplicar(sessao: Session, pedido, restaurant_id: uuid.UUID, novo_status: str):
    """`apply` sem as duas conversas externas do fim do metodo.

    O estorno fala com o Mercado Pago e o aviso fala com a Meta, e os dois
    rodam DEPOIS do commit de proposito. Nenhum dos dois participa da corrida
    que este arquivo mede — e deixa-los ligados faria o teste depender de
    rede.
    """
    servico = OrderStatusChangeService(sessao)
    with patch.object(OrderStatusChangeService, "_notify_customer_on_whatsapp", return_value=None), \
         patch.object(OrderStatusChangeService, "_refund_terminal_order", return_value=None):
        return servico.apply(
            order=pedido,
            restaurant_id=restaurant_id,
            new_status=novo_status,
            note=None,
            changed_by="teste",
            requester="teste",
            route="teste",
            idempotency_key=None,
        )


class TestALinhaEhTravada:
    def test_a_segunda_sessao_nao_consegue_a_mesma_linha(self, cenario):
        """Prova que e um lock de linha, e nao so uma releitura.

        `NOWAIT` em vez de esperar: o teste precisa de um veredito, e esperar
        de verdade so daria um teste lento que trava a suite se o lock nao
        existir.
        """
        travado = OrderRepository(cenario.a).lock_for_status_change(
            cenario.order_id, cenario.restaurant_id
        )
        assert travado is not None

        with pytest.raises(OperationalError):
            cenario.b.execute(
                text("SELECT id FROM orders WHERE id = :o FOR UPDATE NOWAIT"),
                {"o": cenario.order_id},
            )

    def test_sem_o_lock_a_segunda_sessao_pega_a_linha(self, cenario):
        """O outro lado da prova: o `NOWAIT` acima falha por causa do LOCK, e
        nao porque aquela linha e inalcancavel a partir da sessao B."""
        assert cenario.b.execute(
            text("SELECT id FROM orders WHERE id = :o FOR UPDATE NOWAIT"),
            {"o": cenario.order_id},
        ).scalar_one() == cenario.order_id


class TestAInstanciaEhRecarregada:
    def test_o_objeto_ja_carregado_recebe_o_status_do_banco(self, cenario):
        """Sem `populate_existing`, este teste falha e o conserto e um no-op.

        A sessao A carrega o pedido em `ready` (e o guarda no identity map), a
        sessao B move para `completed` e commita, e A trava a linha. O objeto
        que A tem na mao TEM que passar a dizer `completed`.
        """
        pedido_em_a = cenario.a.get(Order, cenario.order_id)
        assert pedido_em_a.status == "ready"

        cenario.b.execute(
            text("UPDATE orders SET status = 'completed' WHERE id = :o"),
            {"o": cenario.order_id},
        )
        cenario.b.commit()

        recarregado = OrderRepository(cenario.a).lock_for_status_change(
            cenario.order_id, cenario.restaurant_id
        )

        assert recarregado is pedido_em_a, "identity map: tem que ser o MESMO objeto"
        assert pedido_em_a.status == "completed", "o objeto continuou com o status velho"

    def test_pedido_de_outro_restaurante_nao_e_travado(self, cenario):
        """O `restaurant_id` do filtro nao e decoracao: sem ele, o metodo
        travaria e devolveria o pedido de qualquer loja."""
        assert (
            OrderRepository(cenario.a).lock_for_status_change(cenario.order_id, uuid.uuid4())
            is None
        )


class TestOPerdedorDaCorridaNaoRodaEfeitoColateral:
    def test_completed_e_depois_cancelled_a_partir_de_ready(self, cenario):
        """A corrida que custa dinheiro, com o vencedor ja commitado.

        As duas transicoes sao validas a partir de `ready`, entao antes do
        lock as duas passavam pela validacao e as duas rodavam o efeito
        colateral: credito de cashback E estorno de cupom no mesmo pedido.
        """
        pedido_em_b = cenario.b.get(Order, cenario.order_id)
        assert pedido_em_b.status == "ready"

        pedido_em_a = cenario.a.get(Order, cenario.order_id)
        _aplicar(cenario.a, pedido_em_a, cenario.restaurant_id, "completed")

        # B chega com o objeto velho na mao, dizendo `ready`.
        with patch.object(CouponService, "reverse_for_order") as estorna_cupom, \
             patch.object(CashbackService, "refund_redemption") as devolve_cashback, \
             pytest.raises(HTTPException) as recusa:
            _aplicar(cenario.b, pedido_em_b, cenario.restaurant_id, "cancelled")

        assert recusa.value.status_code == 409
        estorna_cupom.assert_not_called()
        devolve_cashback.assert_not_called()

    def test_o_pedido_fica_no_status_do_vencedor(self, cenario):
        pedido_em_b = cenario.b.get(Order, cenario.order_id)
        pedido_em_a = cenario.a.get(Order, cenario.order_id)

        _aplicar(cenario.a, pedido_em_a, cenario.restaurant_id, "completed")
        with pytest.raises(HTTPException):
            _aplicar(cenario.b, pedido_em_b, cenario.restaurant_id, "cancelled")

        conferencia = Session(bind=cenario.engine)
        try:
            assert conferencia.execute(
                text("SELECT status FROM orders WHERE id = :o"), {"o": cenario.order_id}
            ).scalar_one() == "completed"
            # O perdedor tambem nao sujou o historico que o cliente ve.
            assert conferencia.execute(
                text(
                    "SELECT count(*) FROM order_status_history "
                    "WHERE order_id = :o AND status = 'cancelled'"
                ),
                {"o": cenario.order_id},
            ).scalar_one() == 0
        finally:
            conferencia.close()


class TestOCaminhoCertoContinuaPassando:
    """A regra do CLAUDE.md: todo `pytest.raises` novo confere que a MESMA
    chamada com o dado CERTO nao levanta. Sem isto, os testes acima ficariam
    verdes com um `apply` que recusa tudo."""

    def test_uma_transicao_sozinha_grava_e_registra(self, cenario):
        pedido = cenario.a.get(Order, cenario.order_id)

        resposta = _aplicar(cenario.a, pedido, cenario.restaurant_id, "completed")

        assert resposta.status == "completed"
        conferencia = Session(bind=cenario.engine)
        try:
            assert conferencia.execute(
                text("SELECT status FROM orders WHERE id = :o"), {"o": cenario.order_id}
            ).scalar_one() == "completed"
            assert conferencia.execute(
                text(
                    "SELECT count(*) FROM order_status_history "
                    "WHERE order_id = :o AND status = 'completed'"
                ),
                {"o": cenario.order_id},
            ).scalar_one() == 1
        finally:
            conferencia.close()
