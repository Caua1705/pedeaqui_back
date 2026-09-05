"""As duas revisões preparadas de `branches`, e o que a armadilha 53 cobra delas.

`20260905_0058` tira as cinco colunas de endereço mortas; `20260905_0059` move a
tarifa de entrega para uma tabela 1:1 opcional. As duas estão em
`alembic/preparadas/`, que o Alembic não lê. A proposta, com tamanho, está em
`docs/modelo-de-dados.md`.

O que se cobra aqui é o mesmo das outras preparadas — o Alembic não as conhece,
elas ainda descrevem o schema real, e elas **rodam** —, com uma diferença que
vale a pena dizer: **estas duas movem DADO**, e a `0057` não movia. Por isso os
testes da `0059` põem linha na mesa antes de rodar a migração. Um `INSERT ...
SELECT` sobre tabela vazia é no-op, e provar só ele seria não provar nada — é a
mesma lição da revisão do cardápio por filial (armadilha 36).

Tudo dentro de uma transação que volta: o Postgres tem DDL transacional, e é por
isso que estes testes podem existir sem estragar o schema de sessão que os
outros testes `db` compartilham. O `finally` não é decoração.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text


RAIZ = Path(__file__).resolve().parent.parent
PREPARADAS = RAIZ / "alembic" / "preparadas"

ENDERECO_MORTO = PREPARADAS / "20260905_0058_branches_perde_o_endereco_morto.py"
TARIFA = PREPARADAS / "20260905_0059_tarifa_de_entrega_sai_de_branches.py"


def _carregar(caminho: Path):
    especificacao = importlib.util.spec_from_file_location(caminho.stem, caminho)
    modulo = importlib.util.module_from_spec(especificacao)
    especificacao.loader.exec_module(modulo)
    return modulo


def _colunas(conexao, tabela: str) -> set[str]:
    return {info["name"] for info in inspect(conexao).get_columns(tabela)}


def _uma_filial(conexao) -> uuid.UUID:
    """Restaurante e filial crus, por SQL, sem passar pelo ORM.

    A fábrica de `fabricas_db` precisa de uma `Session`, e estes testes
    trabalham na CONEXÃO — é nela que o DDL da migração roda, e misturar as duas
    faria a transação que volta deixar de ser uma só.
    """
    restaurante = uuid.uuid4()
    filial = uuid.uuid4()
    conexao.execute(
        text(
            "INSERT INTO restaurants (id, name, slug, is_active) "
            "VALUES (:id, 'Teste das preparadas de branches', :slug, true)"
        ),
        {"id": restaurante, "slug": f"prep-branches-{restaurante.hex[:12]}"},
    )
    conexao.execute(
        text(
            "INSERT INTO branches "
            "(id, restaurant_id, name, slug, address, neighborhood, city, state, "
            " is_open, accepts_delivery, accepts_pickup, "
            " print_customer_copies_delivery, print_production_copies_delivery, "
            " print_customer_copies_pickup, print_production_copies_pickup) "
            "VALUES (:id, :r, 'Centro', :slug, 'Rua A', 'Centro', 'Fortaleza', "
            "'CE', true, true, true, 1, 1, 1, 1)"
        ),
        {"id": filial, "r": restaurante, "slug": f"centro-{filial.hex[:12]}"},
    )
    return filial


@pytest.mark.parametrize("caminho", [ENDERECO_MORTO, TARIFA], ids=lambda p: p.stem)
def test_o_alembic_nao_conhece_estas_revisoes(caminho):
    """A guarda do `git mv` distraído, feita a quem o `upgrade` pergunta."""
    revisao = _carregar(caminho)
    script = ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini")))
    conhecidas = {conhecida.revision for conhecida in script.walk_revisions()}

    assert revisao.revision not in conhecidas, (
        f"{caminho.name} declara revision={revisao.revision!r} e o Alembic a "
        "conhece: ela ESTÁ na cadeia e o próximo `alembic upgrade head` a "
        "aplica, inclusive no container de produção."
    )


@pytest.mark.db
def test_as_colunas_que_a_0058_derruba_existem_hoje(engine_de_teste):
    """A guarda do envelhecimento.

    Uma revisão futura que já tirasse uma delas deixaria esta preparada morrendo
    no meio — e o defeito só apareceria na noite da aplicação.
    """
    revisao = _carregar(ENDERECO_MORTO)

    with engine_de_teste.connect() as conexao:
        existentes = _colunas(conexao, "branches")

    faltando = [nome for nome in revisao.COLUNAS_MORTAS if nome not in existentes]
    assert faltando == [], f"a 0058 quer derrubar colunas que já não existem: {faltando}"
    assert "address_number" in existentes, (
        "`address_number` sumiu de `branches`. Ela é a exceção da 0058 — a única "
        "fonte do número da casa — e o cabeçalho da revisão depende disso."
    )


@pytest.mark.db
def test_a_0058_derruba_as_cinco_e_deixa_address_number(engine_de_teste):
    """As duas metades: o que sai e o que FICA.

    Sem a segunda, uma revisão que derrubasse as seis passaria — e apagaria o
    número da casa do endereço público de toda filial que o tenha preenchido,
    sem nada para pôr no lugar.
    """
    revisao = _carregar(ENDERECO_MORTO)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        with Operations.context(MigrationContext.configure(conexao)):
            revisao.upgrade()

        depois = _colunas(conexao, "branches")
        assert not (set(revisao.COLUNAS_MORTAS) & depois)
        assert "address_number" in depois
        # O conjunto vivo é o que o painel grava e o app lê. Ele não é assunto
        # desta revisão, e o teste diz isso para que uma versão futura dela que
        # os alcançasse caia aqui.
        for viva in ("address", "neighborhood", "city", "state", "zipcode"):
            assert viva in depois, viva

        with Operations.context(MigrationContext.configure(conexao)):
            revisao.downgrade()

        assert set(revisao.COLUNAS_MORTAS) <= _colunas(conexao, "branches")
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_a_0059_move_o_valor_da_filial_que_tem_tarifa(engine_de_teste):
    """Com LINHA na mesa, porque `INSERT ... SELECT` em tabela vazia é no-op.

    É a lição da revisão do cardápio por filial (armadilha 36): a ordem entre a
    cópia e o `DROP` só quebra com dado dentro, e num banco vazio a ordem errada
    passa verde na suíte inteira e derruba o deploy no Júnior.
    """
    tarifa = _carregar(TARIFA)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        filial = _uma_filial(conexao)
        conexao.execute(
            text(
                "UPDATE branches SET delivery_base_fee = 6.50, "
                "delivery_fee_per_km = 1.20, courier_fee_base = 4.00 WHERE id = :id"
            ),
            {"id": filial},
        )

        with Operations.context(MigrationContext.configure(conexao)):
            tarifa.upgrade()

        linha = conexao.execute(
            text(
                "SELECT delivery_base_fee, delivery_fee_per_km, courier_fee_base, "
                "delivery_min_fee FROM branch_delivery_pricing WHERE branch_id = :id"
            ),
            {"id": filial},
        ).one()
        assert float(linha[0]) == 6.50
        assert float(linha[1]) == 1.20
        assert float(linha[2]) == 4.00
        assert linha[3] is None

        assert not (set(tarifa._NOMES) & _colunas(conexao, "branches"))
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_a_0059_nao_cria_linha_de_nulos(engine_de_teste):
    """Filial sem tarifa nenhuma fica SEM LINHA, e esse é o estado da maioria.

    Criar uma linha de nulos para toda filial encheria a tabela de linhas que não
    dizem nada — e, pior, faria "sem linha" nunca acontecer, deixando o caminho
    `LEFT JOIN` sem cobertura até o dia em que acontecesse.
    """
    tarifa = _carregar(TARIFA)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        filial = _uma_filial(conexao)

        with Operations.context(MigrationContext.configure(conexao)):
            tarifa.upgrade()

        quantas = conexao.execute(
            text("SELECT count(*) FROM branch_delivery_pricing WHERE branch_id = :id"),
            {"id": filial},
        ).scalar_one()
        assert quantas == 0
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_o_downgrade_da_0059_devolve_o_valor_para_branches(engine_de_teste):
    """Ida e volta sem perda: as duas direções copiam ANTES de derrubar.

    É o único rollback que este deploy pode precisar de madrugada, e ele não pode
    ser "quase" — voltar sem os valores deixaria toda filial cobrando a taxa
    padrão do restaurante em vez da dela.
    """
    tarifa = _carregar(TARIFA)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        filial = _uma_filial(conexao)
        conexao.execute(
            text("UPDATE branches SET delivery_base_fee = 6.50 WHERE id = :id"),
            {"id": filial},
        )

        with Operations.context(MigrationContext.configure(conexao)):
            tarifa.upgrade()
            tarifa.downgrade()

        de_volta = conexao.execute(
            text("SELECT delivery_base_fee FROM branches WHERE id = :id"),
            {"id": filial},
        ).scalar_one()
        assert float(de_volta) == 6.50
        assert "branch_delivery_pricing" not in inspect(conexao).get_table_names()
    finally:
        transacao.rollback()
        conexao.close()
