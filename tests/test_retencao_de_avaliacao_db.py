"""Retenção do comentário de avaliação — a metade que a anonimização não cobre.

A exclusão de conta já apaga o comentário de quem **tem conta**, alcançando
por `orders.customer_id` (coberto em `test_lgpd_exclusao_de_conta_db.py`).
Este arquivo cobre o resto, e o resto é o **pedido de convidado**:
`orders.customer_id` é nulo, e aquele texto não é alcançável a partir de conta
nenhuma — exatamente a situação do `ai_feedback`.

**O que muda em relação às outras quatro tabelas do expurgo:** aqui é
`UPDATE comment = NULL`, e não `DELETE` da linha. Apagar a linha levaria a
nota junto e reescreveria a média histórica do lojista todo mês, sem nada no
painel explicando por que o ano passado mudou. A nota fica para sempre; só o
texto vence.
"""

from datetime import timedelta

import pytest

from src.models.order_review_model import OrderReview
from src.repositories.order_review_repository import OrderReviewRepository
from src.services.order_review_service import (
    REVIEW_COMMENT_RETENTION_DAYS,
    review_retention_cutoff,
)
from src.utils.security import utcnow
from tests.fabricas_db import (
    criar_filial,
    criar_pedido,
    criar_restaurante,
)


pytestmark = pytest.mark.db


@pytest.fixture
def loja(db):
    restaurante = criar_restaurante(db)
    return restaurante, criar_filial(db, restaurante)


def _avaliar(db, loja, dias_atras: int, comment: str | None, cliente=None):
    restaurante, filial = loja
    pedido = criar_pedido(db, restaurante, filial, cliente=cliente, status="completed")
    avaliacao = OrderReview(
        order_id=pedido.id,
        rating=3,
        comment=comment,
        created_at=utcnow() - timedelta(days=dias_atras),
    )
    db.add(avaliacao)
    db.flush()
    return avaliacao


def _expurgar(db) -> int:
    return OrderReviewRepository(db).clear_comments_created_before(
        review_retention_cutoff(utcnow())
    )


class TestCorteDeRetencao:
    """A conta, isolada."""

    def test_o_corte_fica_no_passado(self):
        agora = utcnow()

        assert review_retention_cutoff(agora) < agora

    def test_e_a_janela_configurada(self):
        agora = utcnow()

        esperado = agora - timedelta(days=REVIEW_COMMENT_RETENTION_DAYS)

        assert review_retention_cutoff(agora) == esperado

    def test_e_mais_longa_que_a_do_ai_feedback(self):
        """As duas janelas existem pelo mesmo motivo e são diferentes de
        propósito: o comentário tem leitor de verdade (o lojista, na aba de
        avaliações) e o feedback do Rapi não tem rota que o leia."""
        from src.services.chat_service import FEEDBACK_RETENTION_DAYS

        assert REVIEW_COMMENT_RETENTION_DAYS > FEEDBACK_RETENTION_DAYS


class TestExpurgoDoComentario:
    def test_comentario_velho_e_apagado(self, db, loja):
        avaliacao = _avaliar(
            db,
            loja,
            dias_atras=REVIEW_COMMENT_RETENTION_DAYS + 1,
            comment="moro na rua das Flores, 200",
        )

        limpos = _expurgar(db)

        db.refresh(avaliacao)
        assert limpos == 1
        assert avaliacao.comment is None

    def test_a_nota_sobrevive_ao_expurgo(self, db, loja):
        """A diferença que separa este expurgo dos outros quatro.

        Se a linha fosse apagada, a média do lojista de doze meses atrás
        mudaria sozinha todo mês.
        """
        avaliacao = _avaliar(
            db, loja, dias_atras=REVIEW_COMMENT_RETENTION_DAYS + 30, comment="demorou"
        )

        _expurgar(db)

        db.refresh(avaliacao)
        assert db.get(OrderReview, avaliacao.id) is not None
        assert avaliacao.rating == 3

    def test_comentario_recente_fica(self, db, loja):
        avaliacao = _avaliar(db, loja, dias_atras=30, comment="veio frio")

        limpos = _expurgar(db)

        db.refresh(avaliacao)
        assert limpos == 0
        assert avaliacao.comment == "veio frio"

    def test_o_texto_do_convidado_tambem_some(self, db, loja):
        """O caso que só a retenção cobre.

        Pedido sem conta: `orders.customer_id` é nulo, então nenhuma exclusão
        de conta jamais alcançaria este texto — hoje ou nunca.
        """
        avaliacao = _avaliar(
            db,
            loja,
            dias_atras=REVIEW_COMMENT_RETENTION_DAYS + 1,
            comment="entregador não achou, ligar no 85999998888",
            cliente=None,
        )
        assert avaliacao.order_id is not None

        _expurgar(db)

        db.refresh(avaliacao)
        assert avaliacao.comment is None

    def test_separa_velho_de_novo_na_mesma_passada(self, db, loja):
        velha = _avaliar(
            db, loja, dias_atras=REVIEW_COMMENT_RETENTION_DAYS + 10, comment="velho"
        )
        nova = _avaliar(db, loja, dias_atras=5, comment="novo")

        limpos = _expurgar(db)

        db.refresh(velha)
        db.refresh(nova)
        assert limpos == 1
        assert velha.comment is None
        assert nova.comment == "novo"

    def test_avaliacao_velha_sem_comentario_nao_e_recontada(self, db, loja):
        """O `WHERE comment IS NOT NULL` existe para o número do log não
        mentir: sem ele, o container relataria "limpou 300" toda noite,
        reescrevendo NULL por NULL."""
        _avaliar(db, loja, dias_atras=REVIEW_COMMENT_RETENTION_DAYS + 1, comment=None)

        assert _expurgar(db) == 0

    def test_banco_sem_avaliacao_velha_nao_e_erro(self, db):
        assert _expurgar(db) == 0
