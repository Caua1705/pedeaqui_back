"""A aba de avaliações do painel: recorte, agregado e isolamento.

Contra o Postgres porque o que se protege é o `JOIN`. `order_reviews` não tem
`restaurant_id` nem `branch_id` — os dois recortes saem de `orders` —, então
uma consulta que esquecesse o `JOIN` devolveria as avaliações de todos os
restaurantes. Isso não é testável com dublê: é exatamente a query.

Duas propriedades do agregado têm teste próprio porque nenhuma das duas é
óbvia lendo a resposta:

- a média sai do **histograma**, não de um `AVG` paralelo, então as barras e
  a média nunca podem se contradizer na mesma tela;
- o filtro `max_rating` **não** entra no agregado, senão o lojista que clica
  em "só notas baixas" vê a média desabar e conclui que a semana piorou.
"""

from datetime import date, timedelta

import pytest

from src.models.order_review_model import OrderReview
from src.services.admin_review_service import AdminReviewService
from src.utils.security import utcnow
from tests.fabricas_db import criar_filial, criar_pedido, criar_restaurante


pytestmark = pytest.mark.db


HOJE = date.today()
ONTEM = HOJE - timedelta(days=1)
AMANHA = HOJE + timedelta(days=1)


class Cenario:
    def __init__(self, db):
        self.db = db
        self.restaurante = criar_restaurante(db)
        self.centro = criar_filial(db, self.restaurante, nome="Centro")
        self.aldeota = criar_filial(db, self.restaurante, nome="Aldeota")

    def avaliar(self, filial, rating: int, problem_tag=None, comment=None, dias_atras=0):
        pedido = criar_pedido(
            self.db, self.restaurante, filial, status="completed"
        )
        avaliacao = OrderReview(
            order_id=pedido.id,
            rating=rating,
            problem_tag=problem_tag,
            comment=comment,
            created_at=utcnow() - timedelta(days=dias_atras),
        )
        self.db.add(avaliacao)
        self.db.flush()
        return avaliacao


@pytest.fixture
def cenario(db) -> Cenario:
    return Cenario(db)


def consultar(db, cenario, branch_id=None, max_rating=None, inicio=ONTEM, fim=AMANHA):
    return AdminReviewService(db).list_reviews(
        restaurant_id=cenario.restaurante.id,
        start_date=inicio,
        end_date=fim,
        branch_id=branch_id,
        max_rating=max_rating,
        limit=50,
        offset=0,
    )


# ---------------------------------------------------------------------------
# O agregado
# ---------------------------------------------------------------------------


def test_periodo_sem_avaliacao_devolve_media_nula(db, cenario):
    """`None` e não `0.0`: média zero seria lida como "todo mundo odiou",
    que é o oposto de "ninguém avaliou"."""
    resposta = consultar(db, cenario)

    assert resposta.summary.total == 0
    assert resposta.summary.average is None
    assert resposta.items == []


def test_o_histograma_traz_as_cinco_notas_mesmo_zeradas(db, cenario):
    """Histograma com buraco obriga cada front a preencher de um jeito."""
    cenario.avaliar(cenario.centro, 5)

    resposta = consultar(db, cenario)

    assert sorted(resposta.summary.by_rating) == [1, 2, 3, 4, 5]
    assert resposta.summary.by_rating[5] == 1
    assert resposta.summary.by_rating[1] == 0


def test_a_media_bate_com_o_histograma(db, cenario):
    """A propriedade que o desenho compra: a média sai das barras.

    Com `COUNT`/`AVG` separados bastaria um `WHERE` divergir para o painel
    mostrar média 4,2 sobre barras que somam 4,6 — e ninguém depura isso
    olhando a tela.
    """
    for nota in (5, 5, 2):
        cenario.avaliar(cenario.centro, nota)

    resumo = consultar(db, cenario).summary

    somado = sum(nota * qtd for nota, qtd in resumo.by_rating.items())
    assert resumo.total == 3
    assert resumo.average == round(somado / resumo.total, 2)
    assert resumo.average == 4.0


def test_as_etiquetas_sao_contadas(db, cenario):
    """A frase que faz o lojista consertar algo: "quase tudo foi atraso"."""
    cenario.avaliar(cenario.centro, 2, problem_tag="atrasou")
    cenario.avaliar(cenario.centro, 1, problem_tag="atrasou")
    cenario.avaliar(cenario.centro, 3, problem_tag="veio_frio")

    resumo = consultar(db, cenario).summary

    assert resumo.by_problem_tag == {"atrasou": 2, "veio_frio": 1}


def test_nota_alta_nao_aparece_nas_etiquetas(db, cenario):
    cenario.avaliar(cenario.centro, 5)

    assert consultar(db, cenario).summary.by_problem_tag == {}


# ---------------------------------------------------------------------------
# O filtro de nota
# ---------------------------------------------------------------------------


def test_max_rating_filtra_a_lista(db, cenario):
    cenario.avaliar(cenario.centro, 5)
    cenario.avaliar(cenario.centro, 2)

    resposta = consultar(db, cenario, max_rating=3)

    assert [item.rating for item in resposta.items] == [2]


def test_max_rating_nao_mexe_no_agregado(db, cenario):
    """Clicar em "só notas baixas" não pode fazer a média do período desabar.

    Se o agregado seguisse o filtro, a tela mostraria média 2,0 e o lojista
    concluiria que a semana inteira foi ruim — quando ele só apertou um
    filtro de lista.
    """
    cenario.avaliar(cenario.centro, 5)
    cenario.avaliar(cenario.centro, 2)

    resposta = consultar(db, cenario, max_rating=3)

    assert len(resposta.items) == 1
    assert resposta.summary.total == 2
    assert resposta.summary.average == 3.5


# ---------------------------------------------------------------------------
# Recorte por filial e isolamento entre restaurantes
# ---------------------------------------------------------------------------


def test_sem_branch_id_soma_as_filiais(db, cenario):
    cenario.avaliar(cenario.centro, 5)
    cenario.avaliar(cenario.aldeota, 1)

    assert consultar(db, cenario).summary.total == 2


def test_branch_id_restringe_a_uma_filial(db, cenario):
    cenario.avaliar(cenario.centro, 5)
    cenario.avaliar(cenario.aldeota, 1)

    resposta = consultar(db, cenario, branch_id=cenario.aldeota.id)

    assert resposta.summary.total == 1
    assert [item.rating for item in resposta.items] == [1]
    assert resposta.items[0].branch_id == cenario.aldeota.id


def test_a_avaliacao_de_outro_restaurante_nao_vaza(db, cenario):
    """O que o `JOIN` com `orders` existe para garantir.

    `order_reviews` não tem `restaurant_id`: se a consulta esquecesse o
    `JOIN`, esta linha apareceria na tela do vizinho.
    """
    vizinho = Cenario(db)
    vizinho.avaliar(vizinho.centro, 1, comment="pessimo")
    cenario.avaliar(cenario.centro, 5)

    resposta = consultar(db, cenario)

    assert resposta.summary.total == 1
    assert [item.rating for item in resposta.items] == [5]


# ---------------------------------------------------------------------------
# O período
# ---------------------------------------------------------------------------


def test_avaliacao_fora_do_periodo_nao_entra(db, cenario):
    cenario.avaliar(cenario.centro, 5, dias_atras=30)

    assert consultar(db, cenario).summary.total == 0


def test_periodo_invertido_responde_400(db, cenario):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as erro:
        consultar(db, cenario, inicio=AMANHA, fim=ONTEM)

    assert erro.value.status_code == 400


def test_periodo_longo_demais_responde_400(db, cenario):
    from fastapi import HTTPException

    from src.services.admin_review_service import MAX_REVIEW_PERIOD_DAYS

    with pytest.raises(HTTPException) as erro:
        consultar(
            db,
            cenario,
            inicio=HOJE - timedelta(days=MAX_REVIEW_PERIOD_DAYS + 5),
            fim=HOJE,
        )

    assert erro.value.status_code == 400


# ---------------------------------------------------------------------------
# O que a resposta NÃO leva
# ---------------------------------------------------------------------------


def test_a_resposta_nao_leva_dado_pessoal_do_cliente(db, cenario):
    """`order_number` basta para o lojista achar o pedido.

    Nome e telefone já estão em `GET /admin/orders/{id}`; repeti-los numa
    segunda tela é superfície a mais sem leitor novo.
    """
    cenario.avaliar(cenario.centro, 5)

    item = consultar(db, cenario).items[0]

    assert item.order_number is not None
    assert not hasattr(item, "customer_name_snapshot")
    assert not hasattr(item, "customer_phone_snapshot")
