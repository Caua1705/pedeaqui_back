"""O backfill da revisão 0016, contra um Postgres de verdade.

A pergunta que este arquivo responde é uma só, e é a que decide se a
migração pode ir para produção: **o link de acompanhamento que já está no
WhatsApp de um cliente continua funcionando depois dela?**

Continua se, e somente se, o hash gravado for o do token que já existia. Um
backfill que regerasse os tokens passaria em qualquer teste de "a coluna
ficou preenchida" e mataria todo link em circulação em silêncio — não há
rota de reemissão (armadilha 19).

Marcado `db`: o backfill é SQL sobre a tabela real.
"""

import hashlib
import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _carregar_revisao():
    caminho = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "20260812_0016_hash_do_token_de_acompanhamento.py"
    )
    spec = importlib.util.spec_from_file_location("revisao_0016", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


REVISAO = _carregar_revisao()


def _pedido_com_token_em_claro(db: Session, restaurante, filial, cliente, token: str) -> uuid.UUID:
    """Grava direto no SQL, e não pelo model.

    O model não mapeia mais `tracking_token` — a coluna existe só até a 0017.
    Um teste do backfill que dependesse do model não conseguiria montar o
    estado de ANTES que ele existe para consertar.
    """
    id_do_pedido = db.execute(
        text(
            "INSERT INTO orders (restaurant_id, branch_id, customer_id, tracking_token, "
            "customer_name_snapshot, customer_phone_snapshot, order_type, status, "
            "payment_method, subtotal, delivery_fee, service_fee, discount_total, total) "
            "VALUES (:restaurant_id, :branch_id, :customer_id, :token, "
            "'Cliente', '85999999999', 'delivery', 'pending', "
            "'cash', 10, 0, 0, 0, 10) RETURNING id"
        ),
        {
            "restaurant_id": restaurante.id,
            "branch_id": filial.id,
            "customer_id": cliente.id,
            "token": token,
        },
    ).scalar_one()
    db.flush()
    return id_do_pedido


def _hash_gravado(db: Session, id_do_pedido: uuid.UUID) -> str | None:
    return db.execute(
        text("SELECT tracking_token_hash FROM orders WHERE id = :id"),
        {"id": id_do_pedido},
    ).scalar_one()


def _token_em_claro(db: Session, id_do_pedido: uuid.UUID) -> str | None:
    return db.execute(
        text("SELECT tracking_token FROM orders WHERE id = :id"),
        {"id": id_do_pedido},
    ).scalar_one()


@pytest.fixture
def loja(db: Session):
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    cliente = fab.criar_cliente(db)
    return restaurante, filial, cliente


def test_o_hash_gravado_e_o_do_token_que_ja_existia(db: Session, loja):
    """O teste que autoriza o deploy: o link antigo continua casando."""
    restaurante, filial, cliente = loja
    token_do_link_ja_enviado = "token-que-o-cliente-tem-no-whatsapp"
    id_do_pedido = _pedido_com_token_em_claro(
        db, restaurante, filial, cliente, token_do_link_ja_enviado
    )

    REVISAO.preencher_os_hashes_que_faltam(db.connection())
    db.flush()

    esperado = hashlib.sha256(token_do_link_ja_enviado.encode("utf-8")).hexdigest()
    assert _hash_gravado(db, id_do_pedido) == esperado


def test_o_token_em_claro_nao_e_alterado_pelo_backfill(db: Session, loja):
    """Regerar o token passaria num teste de "a coluna ficou preenchida" e
    mataria todo link em circulação."""
    restaurante, filial, cliente = loja
    id_do_pedido = _pedido_com_token_em_claro(db, restaurante, filial, cliente, "token-original")

    REVISAO.preencher_os_hashes_que_faltam(db.connection())
    db.flush()

    assert _token_em_claro(db, id_do_pedido) == "token-original"


def test_rodar_duas_vezes_nao_muda_nada(db: Session, loja):
    """Idempotência: a 0017 chama a mesma função para varrer o resíduo, e o
    `alembic upgrade` pode ser repetido depois de um deploy interrompido."""
    restaurante, filial, cliente = loja
    id_do_pedido = _pedido_com_token_em_claro(db, restaurante, filial, cliente, "token-original")

    assert REVISAO.preencher_os_hashes_que_faltam(db.connection()) == 1
    primeiro = _hash_gravado(db, id_do_pedido)

    assert REVISAO.preencher_os_hashes_que_faltam(db.connection()) == 0
    assert _hash_gravado(db, id_do_pedido) == primeiro


def test_o_lote_nao_deixa_pedido_para_tras(db: Session, loja):
    """O laço roda até não sobrar linha. Com lote de 2 e três pedidos, uma
    implementação que fizesse um `LIMIT` só deixaria o terceiro sem hash — e
    aquele cliente perderia o acompanhamento sem nenhum erro aparecer."""
    restaurante, filial, cliente = loja
    ids = [
        _pedido_com_token_em_claro(db, restaurante, filial, cliente, f"token-{numero}")
        for numero in range(3)
    ]

    original = REVISAO.TAMANHO_DO_LOTE
    REVISAO.TAMANHO_DO_LOTE = 2
    try:
        assert REVISAO.preencher_os_hashes_que_faltam(db.connection()) == 3
    finally:
        REVISAO.TAMANHO_DO_LOTE = original

    db.flush()
    for numero, id_do_pedido in enumerate(ids):
        esperado = hashlib.sha256(f"token-{numero}".encode("utf-8")).hexdigest()
        assert _hash_gravado(db, id_do_pedido) == esperado


def test_a_coluna_em_claro_aceita_nulo_depois_da_revisao(db: Session, loja):
    """O que permite ao código novo parar de escrever a coluna em claro antes
    de a 0017 apagá-la. Sem isso, todo pedido criado após o deploy morreria
    no NOT NULL."""
    restaurante, filial, cliente = loja

    id_do_pedido = db.execute(
        text(
            "INSERT INTO orders (restaurant_id, branch_id, customer_id, tracking_token_hash, "
            "customer_name_snapshot, customer_phone_snapshot, order_type, status, "
            "payment_method, subtotal, delivery_fee, service_fee, discount_total, total) "
            "VALUES (:restaurant_id, :branch_id, :customer_id, :hash, "
            "'Cliente', '85999999999', 'delivery', 'pending', "
            "'cash', 10, 0, 0, 0, 10) RETURNING id"
        ),
        {
            "restaurant_id": restaurante.id,
            "branch_id": filial.id,
            "customer_id": cliente.id,
            "hash": hashlib.sha256(b"sem-coluna-em-claro").hexdigest(),
        },
    ).scalar_one()
    db.flush()

    assert _token_em_claro(db, id_do_pedido) is None


def test_o_hash_e_unico(db: Session, loja):
    """A busca por hash precisa da mesma garantia que o `tracking_token`
    UNIQUE dava: dois pedidos com o mesmo hash tornariam a consulta
    ambígua."""
    restaurante, filial, cliente = loja
    mesmo_hash = hashlib.sha256(b"colisao").hexdigest()

    insercao = text(
        "INSERT INTO orders (restaurant_id, branch_id, customer_id, tracking_token_hash, "
        "customer_name_snapshot, customer_phone_snapshot, order_type, status, "
        "payment_method, subtotal, delivery_fee, service_fee, discount_total, total) "
        "VALUES (:restaurant_id, :branch_id, :customer_id, :hash, "
        "'Cliente', '85999999999', 'delivery', 'pending', "
        "'cash', 10, 0, 0, 0, 10)"
    )
    valores = {
        "restaurant_id": restaurante.id,
        "branch_id": filial.id,
        "customer_id": cliente.id,
        "hash": mesmo_hash,
    }

    db.execute(insercao, valores)
    with pytest.raises(IntegrityError, match="ix_orders_tracking_token_hash"):
        db.execute(insercao, valores)
