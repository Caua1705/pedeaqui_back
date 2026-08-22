"""Quem pode avaliar um pedido, quando, e quantas vezes.

Contra o Postgres porque as regras que importam moram no schema tanto quanto
no service: `uq_order_reviews_order_id` é o que transforma "uma vez só" em
fato, e o caminho do token passa pelo `WHERE tracking_token_hash = ...`.

O formato é sempre o mesmo: montar o pedido no estado exato, chamar, e exigir
o status. As recusas são tão testadas quanto os aceites — a rota é pública, e
o que ela recusa é a maior parte do que ela é.
"""

from datetime import timedelta

import pytest
from fastapi import HTTPException

from src.models.order_review_model import OrderReview
from src.models.order_status_history_model import OrderStatusHistory
from src.schemas.order_review_schema import CreateOrderReviewRequest
from src.services.order_review_service import (
    REVIEW_WINDOW_DAYS,
    OrderReviewService,
    completed_at_of,
    review_window_closes_at,
)
from src.utils.security import hash_tracking_token, utcnow
from tests.fabricas_db import criar_filial, criar_pedido, criar_restaurante


pytestmark = pytest.mark.db

TOKEN = "token-de-acompanhamento-do-teste-com-bastante-entropia"


class Cenario:
    """Um restaurante, uma filial e um pedido entregue hoje, com token."""

    def __init__(self, db, status: str = "completed", dias_desde_a_entrega: int = 0):
        self.restaurante = criar_restaurante(db)
        self.filial = criar_filial(db, self.restaurante)
        self.pedido = criar_pedido(
            db,
            self.restaurante,
            self.filial,
            status=status,
            tracking_token=TOKEN,
        )
        # A fábrica pode não hashear o token; aqui o valor é o que a rota vai
        # procurar, então é gravado na forma em que ela busca.
        self.pedido.tracking_token_hash = hash_tracking_token(TOKEN)
        if status == "completed":
            db.add(
                OrderStatusHistory(
                    order_id=self.pedido.id,
                    status="completed",
                    created_at=utcnow() - timedelta(days=dias_desde_a_entrega),
                )
            )
        db.flush()
        db.refresh(self.pedido)


@pytest.fixture
def cenario(db) -> Cenario:
    return Cenario(db)


def avaliar(db, cenario, **campos):
    payload = CreateOrderReviewRequest(**campos)
    return OrderReviewService(db).submit(cenario.restaurante.slug, TOKEN, payload)


# ---------------------------------------------------------------------------
# O caminho feliz
# ---------------------------------------------------------------------------


def test_pedido_entregue_aceita_nota(db, cenario):
    resposta = avaliar(db, cenario, rating=5)

    assert resposta.rating == 5
    assert resposta.problem_tag is None
    assert resposta.comment is None


def test_nota_baixa_aceita_etiqueta_e_comentario(db, cenario):
    resposta = avaliar(
        db,
        cenario,
        rating=2,
        problem_tag="atrasou",
        comment="Chegou uma hora depois do combinado.",
    )

    assert resposta.rating == 2
    assert resposta.problem_tag == "atrasou"
    assert "uma hora depois" in resposta.comment


def test_a_avaliacao_fica_ligada_ao_pedido(db, cenario):
    avaliar(db, cenario, rating=4)

    gravada = db.query(OrderReview).one()
    assert gravada.order_id == cenario.pedido.id


# ---------------------------------------------------------------------------
# Uma vez só — mas editável dentro da janela
# ---------------------------------------------------------------------------


def test_avaliar_de_novo_troca_a_nota_em_vez_de_duplicar(db, cenario):
    """Quem apertou uma estrela por engano precisa de saída.

    E sem isto a segunda gravação bateria em `uq_order_reviews_order_id` e
    viraria erro de integridade em vez de uma troca.
    """
    avaliar(db, cenario, rating=1, problem_tag="veio_errado")

    resposta = avaliar(db, cenario, rating=5)

    assert resposta.rating == 5
    assert db.query(OrderReview).count() == 1


def test_a_troca_limpa_a_etiqueta_que_nao_vale_mais(db, cenario):
    """Subir de 1 para 5 tem que apagar o `problem_tag` antigo.

    Se ele ficasse, o agregado do painel contaria um "atrasou" preso a uma
    nota 5 — exatamente o que a validação do schema existe para impedir na
    escrita, furado pela edição.
    """
    avaliar(db, cenario, rating=2, problem_tag="atrasou")

    avaliar(db, cenario, rating=5)

    assert db.query(OrderReview).one().problem_tag is None


# ---------------------------------------------------------------------------
# O que a rota recusa
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", ["pending", "accepted", "preparing", "ready", "out_for_delivery"]
)
def test_pedido_que_ainda_nao_chegou_responde_409(db, status):
    cenario = Cenario(db, status=status)

    with pytest.raises(HTTPException) as erro:
        avaliar(db, cenario, rating=5)

    assert erro.value.status_code == 409


@pytest.mark.parametrize("status", ["cancelled", "rejected"])
def test_pedido_sem_entrega_nao_e_avaliavel(db, status):
    """`cancelled` e `rejected` ficam de fora porque não houve entrega.

    A nota de um pedido que nunca saiu entraria na média do restaurante
    medindo outra coisa, e reclamação de cancelamento é outro canal.
    """
    cenario = Cenario(db, status=status)

    with pytest.raises(HTTPException) as erro:
        avaliar(db, cenario, rating=1)

    assert erro.value.status_code == 409


def test_fora_da_janela_responde_409(db):
    cenario = Cenario(db, dias_desde_a_entrega=REVIEW_WINDOW_DAYS + 1)

    with pytest.raises(HTTPException) as erro:
        avaliar(db, cenario, rating=5)

    assert erro.value.status_code == 409
    assert str(REVIEW_WINDOW_DAYS) in erro.value.detail


def test_no_ultimo_dia_da_janela_ainda_avalia(db):
    cenario = Cenario(db, dias_desde_a_entrega=REVIEW_WINDOW_DAYS - 1)

    assert avaliar(db, cenario, rating=5).rating == 5


def test_token_errado_responde_404(db, cenario):
    """404 e não 403: 403 confirmaria que aquele pedido existe."""
    payload = CreateOrderReviewRequest(rating=5)

    with pytest.raises(HTTPException) as erro:
        OrderReviewService(db).submit(
            cenario.restaurante.slug, "token-que-nao-existe", payload
        )

    assert erro.value.status_code == 404


def test_token_de_outro_restaurante_responde_404(db, cenario):
    """O escopo do restaurante entra no `WHERE`, e não só o token."""
    outro = criar_restaurante(db)
    db.flush()
    payload = CreateOrderReviewRequest(rating=5)

    with pytest.raises(HTTPException) as erro:
        OrderReviewService(db).submit(outro.slug, TOKEN, payload)

    assert erro.value.status_code == 404


# ---------------------------------------------------------------------------
# A janela, e o marco de onde ela conta
# ---------------------------------------------------------------------------


def test_a_janela_conta_da_entrega_e_nao_da_criacao(db):
    """O pedido esquecido em `out_for_delivery` por três semanas.

    Contando da criação, a janela dele já estaria fechada no instante em que
    deveria abrir, e o cliente veria "prazo encerrado" para um pedido que
    acabou de chegar. Contando da entrega, ele tem os 14 dias inteiros.
    """
    cenario = Cenario(db, dias_desde_a_entrega=0)
    cenario.pedido.created_at = utcnow() - timedelta(days=21)
    db.flush()

    assert avaliar(db, cenario, rating=5).rating == 5


def test_completed_sem_marco_no_historico_deixa_avaliar(db, caplog):
    """Pedido migrado, ou marcado por um caminho que não gravou histórico.

    Sem o marco não dá para saber se a janela venceu. Deixar avaliar é o lado
    barato de errar: o outro é recusar a avaliação de um pedido legítimo por
    causa de uma linha que faltou.
    """
    cenario = Cenario(db, status="completed")
    for registro in list(cenario.pedido.status_history):
        db.delete(registro)
    db.flush()
    db.refresh(cenario.pedido)

    assert avaliar(db, cenario, rating=5).rating == 5


def test_completed_at_pega_o_marco_do_historico(db, cenario):
    marco = completed_at_of(cenario.pedido)

    assert marco is not None
    assert review_window_closes_at(marco) - marco == timedelta(days=REVIEW_WINDOW_DAYS)


# ---------------------------------------------------------------------------
# O log
# ---------------------------------------------------------------------------


def test_o_comentario_nao_vai_para_o_log(db, cenario, caplog):
    """Texto livre é dado pessoal: a pessoa escreve endereço e nome ali.

    A nota vai, o comentário não — mesma regra que já vale para a mensagem
    de chat, que sai como digest.
    """
    import logging

    segredo = "moro no apartamento 302, falar com a Maria"

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        avaliar(db, cenario, rating=2, problem_tag="atrasou", comment=segredo)

    linhas = " ".join(registro.getMessage() for registro in caplog.records)
    # O trecho conferido leva a palavra junto, e não só o número: a linha do
    # log traz o `order_id`, que é um UUID em hexadecimal — e "302" aparece
    # dentro de um UUID aleatório com alguma frequência. Sozinho, o número
    # fazia este teste falhar sem ninguém ter vazado nada.
    assert "apartamento 302" not in linhas
    assert "Maria" not in linhas
    assert "rating=2" in linhas
