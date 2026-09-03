"""O que a revisão 0045 (entregadores) promete ao banco, contra o Postgres.

Quatro garantias, e nenhuma aparece na suíte rápida — todas dependem de o
Postgres estar aplicando constraints e defaults de verdade:

1. **O deploy não muda operação.** Filial existente não ganha taxa de
   entregador: as duas colunas nascem nulas ("não configurado"), e nulo
   nunca vira zero por conta própria.
2. **Um pedido tem no máximo UMA atribuição ativa.** É o índice parcial em
   `order_id WHERE unassigned_at IS NULL`. Sem ele, dois motoboys sairiam
   com o mesmo pedido na lista, e o histórico somaria a taxa duas vezes.
3. **O mesmo telefone não é cadastrado duas vezes na mesma filial** —
   exceto se o anterior foi excluído (`deleted_at`), porque o motoboy que
   saiu e voltou é caso normal.
4. **O hash do link é único.** Dois entregadores com o mesmo link seriam
   uma credencial abrindo dois cadastros.

O estado do schema aqui já é o de depois da revisão — a fixture `db` roda
`alembic upgrade head`.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _filial_criada_pelo_banco(db: Session, restaurante) -> uuid.UUID:
    """Filial por SQL cru: o model tem defaults do lado do Python, e uma
    filial criada por ele passaria verde sobre uma migração sem default."""
    return db.execute(
        text(
            "INSERT INTO branches "
            "(restaurant_id, name, slug, address, neighborhood, city, state) "
            "VALUES (:restaurant_id, 'Centro', :slug, 'Rua A, 100', "
            "'Centro', 'Fortaleza', 'CE') "
            "RETURNING id"
        ),
        {"restaurant_id": restaurante.id, "slug": f"centro-{restaurante.slug}"},
    ).scalar_one()


def _inserir_entregador(
    db: Session,
    restaurante,
    filial_id,
    telefone: str = "85999990000",
    link_hash: str | None = None,
    deleted: bool = False,
) -> uuid.UUID:
    return db.execute(
        text(
            "INSERT INTO couriers "
            "(restaurant_id, branch_id, name, phone, access_link_hash, deleted_at) "
            "VALUES (:restaurant_id, :branch_id, 'Zé', :phone, :link_hash, "
            "CASE WHEN :deleted THEN now() ELSE NULL END) "
            "RETURNING id"
        ),
        {
            "restaurant_id": restaurante.id,
            "branch_id": filial_id,
            "phone": telefone,
            "link_hash": link_hash,
            "deleted": deleted,
        },
    ).scalar_one()


def _atribuir(db: Session, order_id, courier_id, fechada: bool = False) -> None:
    db.execute(
        text(
            "INSERT INTO courier_assignments (order_id, courier_id, unassigned_at) "
            "VALUES (:order_id, :courier_id, CASE WHEN :fechada THEN now() ELSE NULL END)"
        ),
        {"order_id": order_id, "courier_id": courier_id, "fechada": fechada},
    )
    db.flush()


class TestODeployNaoMudaOperacao:
    def test_filial_nasce_sem_taxa_de_entregador(self, db: Session):
        """Nulo é "ninguém configurou". Zero seria "o motoboy trabalha de
        graça", gravado em nome de um lojista que nunca abriu a tela."""
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)

        linha = db.execute(
            text("SELECT courier_fee_base, courier_fee_per_km FROM branches WHERE id = :id"),
            {"id": filial_id},
        ).one()

        assert linha == (None, None)

    def test_entregador_nasce_ativo_sem_acesso_e_nao_excluido(self, db: Session):
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)
        courier_id = _inserir_entregador(db, restaurante, filial_id)

        linha = db.execute(
            text(
                "SELECT is_active, access_link_hash, access_code_hash, deleted_at "
                "FROM couriers WHERE id = :id"
            ),
            {"id": courier_id},
        ).one()

        assert linha == (True, None, None, None)


class TestUmaAtribuicaoAtivaPorPedido:
    def test_segunda_atribuicao_ativa_do_mesmo_pedido_e_recusada(self, db: Session):
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial)
        ze = _inserir_entregador(db, restaurante, filial.id, "85999990001")
        tonho = _inserir_entregador(db, restaurante, filial.id, "85999990002")
        _atribuir(db, pedido.id, ze)

        with pytest.raises(IntegrityError):
            _atribuir(db, pedido.id, tonho)

    def test_e_a_recusa_acima_e_da_atribuicao_ativa(self, db: Session):
        """Fechada a anterior, o mesmo pedido aceita outra: é a reatribuição.
        Sem esta metade, um UNIQUE em `order_id` sem o `WHERE` passaria no
        teste acima e recusaria toda reatribuição."""
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial)
        ze = _inserir_entregador(db, restaurante, filial.id, "85999990001")
        tonho = _inserir_entregador(db, restaurante, filial.id, "85999990002")
        _atribuir(db, pedido.id, ze, fechada=True)

        _atribuir(db, pedido.id, tonho)

        ativas = db.execute(
            text(
                "SELECT count(*) FROM courier_assignments "
                "WHERE order_id = :order_id AND unassigned_at IS NULL"
            ),
            {"order_id": pedido.id},
        ).scalar_one()
        assert ativas == 1


class TestTelefoneUnicoPorFilial:
    def test_mesmo_telefone_na_mesma_filial_e_recusado(self, db: Session):
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)
        _inserir_entregador(db, restaurante, filial_id, "85999990000")

        with pytest.raises(IntegrityError):
            _inserir_entregador(db, restaurante, filial_id, "85999990000")

    def test_mesmo_telefone_em_outra_filial_e_outro_cadastro(self, db: Session):
        """Quem serve duas lojas tem dois cadastros — é a decisão de 1.1."""
        restaurante = fab.criar_restaurante(db)
        centro = _filial_criada_pelo_banco(db, restaurante)
        aldeota = fab.criar_filial(db, restaurante, "Aldeota")

        _inserir_entregador(db, restaurante, centro, "85999990000")
        _inserir_entregador(db, restaurante, aldeota.id, "85999990000")

    def test_excluido_libera_o_telefone(self, db: Session):
        """O motoboy que saiu e voltou é recadastrado, não ressuscitado: o
        cadastro antigo continua excluído, com o histórico dele intacto."""
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)
        _inserir_entregador(db, restaurante, filial_id, "85999990000", deleted=True)

        _inserir_entregador(db, restaurante, filial_id, "85999990000")


class TestOLinkEUnico:
    def test_dois_entregadores_com_o_mesmo_hash_de_link_e_recusado(self, db: Session):
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)
        _inserir_entregador(db, restaurante, filial_id, "85999990001", link_hash="h1")

        with pytest.raises(IntegrityError):
            _inserir_entregador(db, restaurante, filial_id, "85999990002", link_hash="h1")

    def test_e_dois_sem_link_convivem(self, db: Session):
        """Nulo não colide com nulo: dois cadastros sem acesso gerado são o
        estado normal entre criar e gerar o código."""
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)
        _inserir_entregador(db, restaurante, filial_id, "85999990001")
        _inserir_entregador(db, restaurante, filial_id, "85999990002")


class TestATaxaNaoAceitaNegativo:
    def test_taxa_negativa_na_filial_e_recusada(self, db: Session):
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)

        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE branches SET courier_fee_base = -1 WHERE id = :id"),
                {"id": filial_id},
            )
            db.flush()
