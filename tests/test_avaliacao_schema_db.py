"""O schema de `order_reviews`: o que o banco recusa sozinho.

Estes testes não passam por service nenhum. Eles gravam direto e exigem que o
Postgres recuse — porque as três regras abaixo são as que precisam continuar
valendo quando alguém escrever um segundo caminho de escrita, um script de
importação ou um `UPDATE` à mão no incidente das 3h.

Regra que a suíte não pegaria de outro jeito: `-m db` obrigatório. Um dublê
de banco não tem CHECK, não tem UNIQUE e não tem FK — validar a nota só no
Pydantic prova que o schema aceita, não que ele recusa.
"""

import uuid

import pytest
from sqlalchemy.exc import DataError, IntegrityError

from src.core.constants import REVIEW_PROBLEM_TAGS
from src.models.order_review_model import OrderReview
from tests.fabricas_db import criar_filial, criar_pedido, criar_restaurante


pytestmark = pytest.mark.db


@pytest.fixture
def pedido(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    return criar_pedido(db, restaurante, filial, status="completed")


def _gravar(db, pedido, **campos) -> OrderReview:
    avaliacao = OrderReview(order_id=pedido.id, **campos)
    db.add(avaliacao)
    db.flush()
    return avaliacao


class TestNota:
    def test_de_um_a_cinco_passa(self, db, pedido):
        avaliacao = _gravar(db, pedido, rating=5)

        assert avaliacao.rating == 5

    @pytest.mark.parametrize("nota", [0, 6, -1, 100])
    def test_fora_da_faixa_o_banco_recusa(self, db, pedido, nota):
        """O CHECK existe porque o schema do Pydantic não é o único caminho
        de escrita — e nota 0 numa média é indistinguível de nota baixa."""
        with pytest.raises((IntegrityError, DataError)):
            _gravar(db, pedido, rating=nota)


class TestUmaAvaliacaoPorPedido:
    def test_a_segunda_avaliacao_do_mesmo_pedido_e_recusada(self, db, pedido):
        """A regra de "uma vez só", escrita onde não depende de ninguém
        lembrar: duas requisições simultâneas do mesmo token gravariam duas
        notas para o mesmo pedido se isto morasse só no service."""
        _gravar(db, pedido, rating=5)

        with pytest.raises(IntegrityError):
            _gravar(db, pedido, rating=1)

    def test_pedidos_diferentes_avaliam_separado(self, db, pedido):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        outro = criar_pedido(db, restaurante, filial, status="completed")

        _gravar(db, pedido, rating=5)
        _gravar(db, outro, rating=2)

        assert db.query(OrderReview).count() == 2


class TestEtiquetaDeProblema:
    def test_nulo_e_o_normal(self, db, pedido):
        """Só se pergunta o que deu errado a quem disse que deu errado."""
        avaliacao = _gravar(db, pedido, rating=5)

        assert avaliacao.problem_tag is None

    @pytest.mark.parametrize("etiqueta", REVIEW_PROBLEM_TAGS)
    def test_toda_etiqueta_da_constante_e_aceita_pelo_banco(self, db, pedido, etiqueta):
        """A metade que importa da armadilha 15.

        `REVIEW_PROBLEM_TAGS` espelha o CHECK `ck_order_reviews_problem_tag`.
        Etiqueta que exista só na constante passa pela validação do schema e
        morre no INSERT — em produção, na avaliação de um cliente de verdade,
        e não aqui. Este teste é o que faz as duas listas mudarem juntas.
        """
        avaliacao = _gravar(db, pedido, rating=2, problem_tag=etiqueta)

        assert avaliacao.problem_tag == etiqueta

    def test_etiqueta_desconhecida_o_banco_recusa(self, db, pedido):
        with pytest.raises(IntegrityError):
            _gravar(db, pedido, rating=2, problem_tag="banana")


class TestVinculoComOPedido:
    def test_pedido_inexistente_e_recusado(self, db):
        """FK sem `ON DELETE`: avaliação órfã é pior que o erro que avisa."""
        avaliacao = OrderReview(order_id=uuid.uuid4(), rating=5)
        db.add(avaliacao)

        with pytest.raises(IntegrityError):
            db.flush()
