"""A revisão 0015, executada contra um Postgres de verdade.

Ela escolhe a filial do usuário do agente de impressão por dedução — "a
única filial que tem setor de impressão ativo" — e o caso interessante é o
que ela NÃO faz: empatada, não escolhe. Um teste que só cobrisse o caminho
feliz deixaria de fora exatamente a decisão que importa.

Marcado `db` porque o corpo da revisão é um bloco `DO $$` de PL/pgSQL: não
há como executá-lo fora do Postgres.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.models.admin_user_model import AdminUser
from src.models.printing_sector_model import PrintingSector
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _carregar_revisao():
    """O nome do arquivo começa com dígito, então `import` não serve."""
    caminho = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "20260812_0015_filial_do_usuario_do_agente_de_impressao.py"
    )
    spec = importlib.util.spec_from_file_location("revisao_0015", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


REVISAO = _carregar_revisao()


def _criar_agente(db: Session, restaurante, branch_id=None) -> AdminUser:
    agente = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=branch_id,
        name="Impressora Junior",
        email=REVISAO.EMAIL_DO_AGENTE,
        password_hash="$2b$12$" + "x" * 53,
        role="attendant",
        is_active=True,
    )
    db.add(agente)
    db.flush()
    return agente


def _criar_setor(db: Session, filial, nome: str = "Cozinha", is_active: bool = True) -> PrintingSector:
    setor = PrintingSector(branch_id=filial.id, name=nome, is_active=is_active, sort_order=0)
    db.add(setor)
    db.flush()
    return setor


def _rodar(db: Session) -> None:
    db.execute(text(REVISAO.SQL_PRENDE_A_FILIAL))
    db.flush()
    db.expire_all()


def test_uma_filial_com_setor_ativo_e_a_escolhida(db: Session):
    restaurante = fab.criar_restaurante(db)
    com_impressora = fab.criar_filial(db, restaurante, "Centro")
    fab.criar_filial(db, restaurante, "Aldeota")
    _criar_setor(db, com_impressora)
    agente = _criar_agente(db, restaurante)

    _rodar(db)

    assert agente.branch_id == com_impressora.id


def test_duas_filiais_com_setor_deixam_o_branch_id_nulo(db: Session):
    """O caso do Júnior, se as duas lojas tiverem setor cadastrado.

    Não escolhe. Com nulo o agente imprime as próprias comandas junto com as
    da outra loja; com a filial errada ele pararia de imprimir as próprias em
    silêncio, que é o modo de falha caro.
    """
    restaurante = fab.criar_restaurante(db)
    uma = fab.criar_filial(db, restaurante, "Centro")
    outra = fab.criar_filial(db, restaurante, "Aldeota")
    _criar_setor(db, uma)
    _criar_setor(db, outra)
    agente = _criar_agente(db, restaurante)

    _rodar(db)

    assert agente.branch_id is None


def test_nenhuma_filial_com_setor_deixa_o_branch_id_nulo(db: Session):
    restaurante = fab.criar_restaurante(db)
    fab.criar_filial(db, restaurante, "Centro")
    agente = _criar_agente(db, restaurante)

    _rodar(db)

    assert agente.branch_id is None


def test_setor_desativado_nao_conta(db: Session):
    """Setor desativado é impressora que saiu de uso — não diz onde a máquina
    está hoje."""
    restaurante = fab.criar_restaurante(db)
    ativa = fab.criar_filial(db, restaurante, "Centro")
    desativada = fab.criar_filial(db, restaurante, "Aldeota")
    _criar_setor(db, ativa)
    _criar_setor(db, desativada, is_active=False)
    agente = _criar_agente(db, restaurante)

    _rodar(db)

    assert agente.branch_id == ativa.id


def test_quem_ja_tem_filial_nao_e_tocado(db: Session):
    """Idempotência: o `alembic upgrade` pode rodar de novo, e uma filial
    corrigida à mão depois não pode ser sobrescrita pela dedução."""
    restaurante = fab.criar_restaurante(db)
    escolhida_a_mao = fab.criar_filial(db, restaurante, "Aldeota")
    com_setor = fab.criar_filial(db, restaurante, "Centro")
    _criar_setor(db, com_setor)
    agente = _criar_agente(db, restaurante, branch_id=escolhida_a_mao.id)

    _rodar(db)

    assert agente.branch_id == escolhida_a_mao.id


def test_banco_sem_o_usuario_nao_levanta_erro(db: Session):
    """Todo banco que não seja o de produção cai aqui — inclusive o do CI.

    Erro nesta revisão viraria loop de restart do container, porque o
    `docker-entrypoint.sh` roda o upgrade com `set -e` antes do Uvicorn
    (armadilha 5).
    """
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante, "Centro")
    _criar_setor(db, filial)

    _rodar(db)

    ninguem = db.query(AdminUser).filter(AdminUser.email == REVISAO.EMAIL_DO_AGENTE).first()
    assert ninguem is None


def test_o_downgrade_devolve_o_nulo(db: Session):
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante, "Centro")
    _criar_setor(db, filial)
    agente = _criar_agente(db, restaurante)

    _rodar(db)
    assert agente.branch_id == filial.id

    db.execute(
        text("UPDATE admin_users SET branch_id = NULL WHERE email = :email"),
        {"email": REVISAO.EMAIL_DO_AGENTE},
    )
    db.flush()
    db.expire_all()

    assert agente.branch_id is None


def test_a_filial_de_outro_restaurante_nao_entra_na_conta(db: Session):
    """`printing_sectors` não tem restaurant_id: ele chega pelo join com
    `branches`. Sem esse join, o setor do restaurante vizinho contaria como
    empate e a revisão viraria no-op sem motivo."""
    restaurante = fab.criar_restaurante(db)
    minha = fab.criar_filial(db, restaurante, "Centro")
    _criar_setor(db, minha)

    vizinho = fab.criar_restaurante(db, "Vizinho")
    filial_do_vizinho = fab.criar_filial(db, vizinho, "Unica")
    _criar_setor(db, filial_do_vizinho)

    agente = _criar_agente(db, restaurante)

    _rodar(db)

    assert agente.branch_id == minha.id


def test_o_email_do_agente_e_o_que_a_revisao_procura():
    """Guarda de digitação: o e-mail é a única chave que a revisão tem."""
    assert REVISAO.EMAIL_DO_AGENTE == "impressora.junior@pederapidex.com"
