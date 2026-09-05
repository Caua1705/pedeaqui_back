"""A revisão preparada `20260905_0057`, e as três coisas que a armadilha 53 cobra.

Migração escrita e não aplicada é útil — tira a escolha do meio da madrugada — e
perigosa por dois motivos independentes: ela pode aplicar sozinha (um `git mv`
distraído põe o arquivo em `versions/` e o próximo `alembic upgrade head` do
entrypoint a executa **em produção**), e ela pode envelhecer calada (foi escrita
contra o schema de hoje, e uma revisão futura pode mudar o que ela descreve).

O que se cobra aqui:

1. **o Alembic NÃO a conhece** — a pergunta é feita ao `ScriptDirectory`, que é
   quem o `upgrade` consulta, e não ao caminho do arquivo. Este primeiro está em
   `tests/test_alinhamento_orm_schema.py::test_nada_em_preparadas_esta_na_cadeia`,
   que varre o diretório inteiro; com `preparadas/` vazio ele passava sem afirmar
   nada, e volta a ter substância neste commit;
2. **ela ainda descreve o schema real** — a CHECK que ela derruba existe hoje,
   com os dois valores que ela espera;
3. **ela RODA** — contra o Postgres 17 de teste, dentro de uma transação que
   volta. Sem isto, "pronta" quer dizer "nunca executada", e o primeiro lugar
   onde ela roda de verdade acaba sendo produção, de madrugada, com a API fora
   do ar.

O 3 é o que muda a natureza da entrega, e o Postgres tem DDL transacional — é
por isso que ele pode existir sem estragar o schema de sessão que os outros
testes `db` compartilham. O `finally` não é decoração.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


RAIZ = Path(__file__).resolve().parent.parent
PREPARADA = (
    RAIZ
    / "alembic"
    / "preparadas"
    / "20260905_0057_indexacao_do_cardapio_no_custo_de_ia.py"
)

#: O que a CHECK aceita hoje, e o que ela passa a aceitar. Escrito aqui e não
#: lido da revisão de propósito: um teste que lesse a resposta do arquivo que
#: está testando concordaria consigo mesmo.
SURFACES_DE_HOJE = {"text", "voice"}
SURFACES_DEPOIS = {"text", "voice", "indexing"}


def _carregar_revisao():
    especificacao = importlib.util.spec_from_file_location(PREPARADA.stem, PREPARADA)
    modulo = importlib.util.module_from_spec(especificacao)
    especificacao.loader.exec_module(modulo)
    return modulo


def _definicao_da_check(conexao) -> str:
    return conexao.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_ai_usage_events_surface'"
        )
    ).scalar_one()


def _um_restaurante(conexao) -> uuid.UUID:
    """Um restaurante cru, por SQL, sem passar pelo ORM.

    A fábrica de `fabricas_db` precisa de uma `Session`, e este arquivo trabalha
    na CONEXÃO — é nela que o DDL da migração roda, e misturar as duas faria a
    transação que volta deixar de ser uma só.
    """
    identificador = uuid.uuid4()
    conexao.execute(
        text(
            "INSERT INTO restaurants (id, name, slug, is_active) "
            "VALUES (:id, 'Teste da revisao preparada', :slug, true)"
        ),
        {"id": identificador, "slug": f"preparada-{identificador.hex[:12]}"},
    )
    return identificador


def _gravar_indexacao(conexao, restaurant_id: uuid.UUID) -> None:
    conexao.execute(
        text(
            "INSERT INTO ai_usage_events "
            "(restaurant_id, surface, model, input_tokens, output_tokens, cost_usd) "
            "VALUES (:r, 'indexing', 'text-embedding-3-small', 42, 0, 0.000001)"
        ),
        {"r": restaurant_id},
    )


def test_o_alembic_nao_conhece_esta_revisao():
    """A guarda do `git mv` distraído, feita a quem o `upgrade` pergunta.

    Perguntar ao caminho do arquivo responderia outra coisa: o que decide se ela
    é aplicada é o `ScriptDirectory`, e é ele que tem que dizer "não conheço".
    """
    revisao = _carregar_revisao()
    script = ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini")))
    conhecidas = {conhecida.revision for conhecida in script.walk_revisions()}

    assert revisao.revision not in conhecidas, (
        f"{PREPARADA.name} declara revision={revisao.revision!r} e o Alembic a "
        "conhece: ela ESTÁ na cadeia e o próximo `alembic upgrade head` a "
        "aplica, inclusive no container de produção."
    )


@pytest.mark.db
def test_ela_ainda_descreve_o_schema_de_hoje(engine_de_teste):
    """A guarda do envelhecimento: a CHECK que ela derruba existe, com os dois
    valores que ela espera.

    Uma revisão futura que mexesse em `surface` deixaria esta preparada
    descrevendo um banco que não existe mais — e o defeito só apareceria na
    noite da aplicação.
    """
    with engine_de_teste.connect() as conexao:
        definicao = _definicao_da_check(conexao)

    for valor in SURFACES_DE_HOJE:
        assert f"'{valor}'" in definicao, definicao
    assert "'indexing'" not in definicao, (
        "a CHECK já aceita 'indexing': a revisão preparada foi aplicada, ou "
        "alguém a reescreveu à mão no banco (armadilha 33)."
    )


@pytest.mark.db
def test_o_upgrade_abre_a_terceira_superficie(engine_de_teste):
    """Ela RODA, e o efeito é o que o cabeçalho promete.

    Duas afirmações e não uma: a definição da CHECK passa a citar os três
    valores, **e** um INSERT de indexação passa a ser aceito. A primeira sozinha
    provaria que o texto mudou; é a segunda que prova que o banco mudou de
    comportamento.
    """
    revisao = _carregar_revisao()

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        with Operations.context(MigrationContext.configure(conexao)):
            revisao.upgrade()

        definicao = _definicao_da_check(conexao)
        for valor in SURFACES_DEPOIS:
            assert f"'{valor}'" in definicao, definicao

        _gravar_indexacao(conexao, _um_restaurante(conexao))
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_antes_do_upgrade_a_indexacao_e_recusada(engine_de_teste):
    """A contraprova do teste acima.

    Sem ela, uma revisão que não fizesse nada passaria: o INSERT de indexação
    teria sido aceito o tempo todo e ninguém saberia. É esta que prova que a
    CHECK de hoje é o que separa os dois estados — e, de passagem, que o código
    de gravação **não pode** ser mesclado antes desta revisão entrar.
    """
    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        restaurante = _um_restaurante(conexao)
        with pytest.raises(IntegrityError, match="ck_ai_usage_events_surface"):
            _gravar_indexacao(conexao, restaurante)
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_o_downgrade_volta_ao_estado_de_hoje_e_leva_as_linhas_de_indexacao(engine_de_teste):
    """Voltar tem que dar exatamente o estado de hoje, e o `DELETE` não é sobra.

    `ADD CONSTRAINT ... CHECK` **valida a tabela inteira**: com uma linha
    `surface = 'indexing'` gravada, recriar a CHECK antiga falharia no meio e
    deixaria a tabela sem CHECK nenhuma — pior que qualquer um dos dois estados
    que o downgrade alterna. Por isso ele apaga antes.

    É o único rollback que este deploy pode precisar de madrugada, e ele é
    exercitado aqui com uma linha de indexação na mesa — porque sem linha o
    `DELETE` seria no-op e o teste passaria sem tocar no que importa.
    """
    revisao = _carregar_revisao()

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        with Operations.context(MigrationContext.configure(conexao)):
            revisao.upgrade()

        restaurante = _um_restaurante(conexao)
        _gravar_indexacao(conexao, restaurante)

        with Operations.context(MigrationContext.configure(conexao)):
            revisao.downgrade()

        definicao = _definicao_da_check(conexao)
        assert "'indexing'" not in definicao, definicao
        for valor in SURFACES_DE_HOJE:
            assert f"'{valor}'" in definicao, definicao

        sobraram = conexao.execute(
            text("SELECT count(*) FROM ai_usage_events WHERE surface = 'indexing'")
        ).scalar_one()
        assert sobraram == 0
    finally:
        transacao.rollback()
        conexao.close()
