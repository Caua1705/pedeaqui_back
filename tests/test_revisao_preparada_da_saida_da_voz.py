"""A revisão preparada `20260906_0060`, e as três coisas que a armadilha 53 cobra.

Migração escrita e não aplicada é útil — tira a escolha do meio da madrugada — e
perigosa por dois motivos independentes: ela pode aplicar sozinha (um `git mv`
distraído põe o arquivo em `versions/` e o próximo `alembic upgrade head` do
entrypoint a executa **em produção**), e ela pode envelhecer calada (foi escrita
contra o schema de hoje).

**Esta é a única das quatro preparadas que APAGA DADO**, e por isso ela ganha um
quarto teste que as outras não têm: o de que as linhas de custo da voz em
`ai_usage_events` **sobrevivem** ao upgrade. É a promessa escrita no cabeçalho
dela e em `src/schemas/ai_usage_schema.py` — o dinheiro fica, o detalhe por
sessão não — e uma promessa sobre dado de produção que ninguém exercita é uma
frase.

O que se cobra aqui:

1. **o Alembic NÃO a conhece** — a pergunta é feita ao `ScriptDirectory`, que é
   quem o `upgrade` consulta, e não ao caminho do arquivo;
2. **ela ainda descreve o schema real** — a tabela, as duas colunas, a CHECK e o
   índice único que ela derruba existem hoje, com os nomes que ela usa;
3. **ela RODA** — contra o Postgres 17 de teste, dentro de uma transação que
   volta. Sem isto, "pronta" quer dizer "nunca executada", e o primeiro lugar
   onde ela roda de verdade acaba sendo produção, de madrugada, com a API fora
   do ar;
4. **ela não leva o dinheiro junto.**

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
from sqlalchemy import inspect, text


RAIZ = Path(__file__).resolve().parent.parent
PREPARADA = RAIZ / "alembic" / "preparadas" / "20260906_0060_a_voz_sai_do_banco.py"

#: O que ela derruba, escrito aqui e não lido da revisão de propósito: um teste
#: que lesse a resposta do arquivo que está testando concordaria consigo mesmo.
TABELA = "ai_voice_sessions"
COLUNAS = (("ai_usage_events", "voice_session_id"), ("restaurant_settings", "voice_enabled"))
CHECK = "ck_ai_usage_events_voice_has_session"
UNIQUE = "ux_ai_usage_events_voice_session"


def _carregar_revisao():
    especificacao = importlib.util.spec_from_file_location(PREPARADA.stem, PREPARADA)
    modulo = importlib.util.module_from_spec(especificacao)
    especificacao.loader.exec_module(modulo)
    return modulo


def _existe_constraint(conexao, nome: str) -> bool:
    return conexao.execute(
        text("SELECT count(*) FROM pg_constraint WHERE conname = :nome"), {"nome": nome}
    ).scalar_one() > 0


def _existe_indice(conexao, nome: str) -> bool:
    return conexao.execute(
        text("SELECT count(*) FROM pg_class WHERE relkind = 'i' AND relname = :nome"),
        {"nome": nome},
    ).scalar_one() > 0


def _colunas(conexao, tabela: str) -> set[str]:
    return {coluna["name"] for coluna in inspect(conexao).get_columns(tabela)}


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
            "VALUES (:id, 'Teste da saida da voz', :slug, true)"
        ),
        {"id": identificador, "slug": f"saida-da-voz-{identificador.hex[:12]}"},
    )
    return identificador


def _semear_uma_sessao_com_custo(conexao, restaurant_id: uuid.UUID) -> None:
    """A linha que produção tem: uma sessão de voz e o custo dela.

    Sem ela o `DROP TABLE` seria sobre tabela vazia, e "o dinheiro sobrevive"
    passaria sem tocar em linha nenhuma — que é o modo de falha que este arquivo
    inteiro existe para não ter.
    """
    sessao_id = uuid.uuid4()
    conexao.execute(
        text(
            "INSERT INTO ai_voice_sessions (id, restaurant_id, expires_at) "
            "VALUES (:id, :r, now() + interval '5 minutes')"
        ),
        {"id": sessao_id, "r": restaurant_id},
    )
    conexao.execute(
        text(
            "INSERT INTO ai_usage_events "
            "(restaurant_id, surface, model, input_tokens, output_tokens, "
            " cost_usd, voice_session_id) "
            "VALUES (:r, 'voice', 'gpt-realtime-mini', 1000, 1000, 0.030000, :s)"
        ),
        {"r": restaurant_id, "s": sessao_id},
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
        "aplica, inclusive no container de produção — apagando a tabela."
    )


@pytest.mark.db
def test_ela_ainda_descreve_o_schema_de_hoje(engine_de_teste):
    """A guarda do envelhecimento: os cinco objetos que ela derruba existem.

    Uma revisão futura que renomeasse a CHECK ou o índice deixaria esta
    preparada morrendo no meio — e o meio, aqui, é depois de um `DROP COLUMN`.
    """
    with engine_de_teste.connect() as conexao:
        assert inspect(conexao).has_table(TABELA)
        for tabela, coluna in COLUNAS:
            assert coluna in _colunas(conexao, tabela), (tabela, coluna)
        assert _existe_constraint(conexao, CHECK)
        assert _existe_indice(conexao, UNIQUE)


@pytest.mark.db
def test_o_upgrade_derruba_os_cinco_objetos(engine_de_teste):
    """Ela RODA, e o efeito é o que o cabeçalho promete.

    Com uma sessão e um evento de custo na mesa: o `DROP TABLE` sobre tabela
    vazia não exercita a FK, que é justamente o que obriga a coluna a cair antes
    da tabela.
    """
    revisao = _carregar_revisao()

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        _semear_uma_sessao_com_custo(conexao, _um_restaurante(conexao))

        with Operations.context(MigrationContext.configure(conexao)):
            revisao.upgrade()

        assert not inspect(conexao).has_table(TABELA)
        for tabela, coluna in COLUNAS:
            assert coluna not in _colunas(conexao, tabela), (tabela, coluna)
        assert not _existe_constraint(conexao, CHECK)
        assert not _existe_indice(conexao, UNIQUE)
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_o_upgrade_NAO_leva_o_custo_ja_gravado(engine_de_teste):
    """A promessa que separa esta revisão de um `DELETE`: o dinheiro fica.

    `ai_usage_events` não é tocada. A linha de `surface = 'voice'` continua lá,
    com o `cost_usd` dela — o que ela perde é o ponteiro para a sessão, que
    deixou de existir. É isso que faz `GET /internal/ai-usage` continuar
    fechando a conta de agosto de 2026 depois da aplicação.
    """
    revisao = _carregar_revisao()

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        restaurante = _um_restaurante(conexao)
        _semear_uma_sessao_com_custo(conexao, restaurante)

        with Operations.context(MigrationContext.configure(conexao)):
            revisao.upgrade()

        linha = conexao.execute(
            text(
                "SELECT count(*), coalesce(sum(cost_usd), 0) FROM ai_usage_events "
                "WHERE surface = 'voice' AND restaurant_id = :r"
            ),
            {"r": restaurante},
        ).one()

        assert linha[0] == 1
        assert float(linha[1]) == pytest.approx(0.03)
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_o_downgrade_recria_a_estrutura_e_nao_devolve_linha(engine_de_teste):
    """Voltar dá a forma de hoje, e uma tabela VAZIA — as duas afirmações.

    A segunda é a que importa na madrugada: quem rodar o downgrade achando que
    ele desfaz a aplicação vai achar a estrutura no lugar e a tabela sem nada
    dentro. Está escrito no cabeçalho da revisão, e aqui está provado.

    E a CHECK volta `NOT VALID`, de propósito: recriá-la estrita falharia sobre
    a linha de `surface = 'voice'` que sobreviveu, cuja chave o upgrade apagou.
    """
    revisao = _carregar_revisao()

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        _semear_uma_sessao_com_custo(conexao, _um_restaurante(conexao))

        with Operations.context(MigrationContext.configure(conexao)):
            revisao.upgrade()
        with Operations.context(MigrationContext.configure(conexao)):
            revisao.downgrade()

        assert inspect(conexao).has_table(TABELA)
        for tabela, coluna in COLUNAS:
            assert coluna in _colunas(conexao, tabela), (tabela, coluna)
        assert _existe_constraint(conexao, CHECK)
        assert _existe_indice(conexao, UNIQUE)

        sobraram = conexao.execute(text(f"SELECT count(*) FROM {TABELA}")).scalar_one()
        assert sobraram == 0, "o downgrade devolveu linha: o cabeçalho promete que não"
    finally:
        transacao.rollback()
        conexao.close()
