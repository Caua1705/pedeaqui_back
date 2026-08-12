"""Fixtures da suíte marcada `db` — a que roda contra um Postgres de verdade.

Nada aqui é importado pela suíte rápida: as fixtures só constroem alguma coisa
quando um teste as pede, e a suíte rápida (`pytest -m "not db"`) não pede
nenhuma. Rodar sem Docker aberto continua funcionando.

Como o schema nasce, e por que não é `Base.metadata.create_all()`: armadilha 24
— o ORM não mapeia as sequences (inclusive a de `order_number`), os DEFAULT nem
os índices criados à mão. O schema sai do DDL de verdade:

    alembic/schema_baseline.sql   foto do banco de produção na revisão 0012
    alembic stamp 20260810_0012   marca essa foto como aplicada
    alembic upgrade head          aplica o que vier depois dela

O `.sql` no meio do caminho não é atalho: a revisão de baseline do Alembic é um
no-op (o schema é anterior ao Alembic), então `upgrade head` sozinho num banco
vazio morre na revisão 0002. Ver o cabeçalho do próprio `schema_baseline.sql`.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session


RAIZ_DO_REPOSITORIO = Path(__file__).resolve().parent.parent
SCHEMA_BASELINE = RAIZ_DO_REPOSITORIO / "alembic" / "schema_baseline.sql"
REVISAO_DO_SCHEMA_BASELINE = "20260810_0012"

# O mesmo banco de docker-compose.test.yml. `TEST_DATABASE_URL` sobrepõe, para
# quem já tem um Postgres 17 na máquina e não quer subir container.
URL_PADRAO = "postgresql+psycopg://pedeaqui:pedeaqui@localhost:55432/pedeaqui_teste"

COMO_SUBIR_O_BANCO = (
    "Suba o Postgres de teste antes:\n"
    "    docker compose -f docker-compose.test.yml up -d\n"
    "ou aponte TEST_DATABASE_URL para outro Postgres 17."
)


def url_do_banco_de_teste() -> str:
    return os.environ.get("TEST_DATABASE_URL", URL_PADRAO)


def recusar_banco_que_nao_e_de_teste(url: str) -> None:
    """A primeira coisa que a fixture de schema faz é apagar o schema inteiro.

    Um `TEST_DATABASE_URL` colado errado — ou um `DATABASE_URL` de produção que
    vazou para a variável — encontraria um DROP SCHEMA public CASCADE do outro
    lado. Duas condições, as duas baratas: o banco tem que ser local e o nome
    tem que terminar em `_teste`.
    """
    partes = urlparse(url)
    if partes.hostname not in ("localhost", "127.0.0.1"):
        raise RuntimeError(
            f"TEST_DATABASE_URL aponta para o host '{partes.hostname}', que não é "
            "local. A suíte `db` apaga o schema inteiro antes de recriá-lo e só "
            "aceita banco na própria máquina."
        )
    if not partes.path.endswith("_teste"):
        raise RuntimeError(
            f"O banco '{partes.path.lstrip('/')}' não termina em '_teste'. A suíte "
            "`db` apaga o schema inteiro e só aceita banco marcado como descartável."
        )


def apagar_e_recriar_o_schema(engine: Engine) -> None:
    """Devolve um banco vazio mesmo depois de uma execução anterior.

    Sem isso, rodar a suíte duas vezes sem `down -v` no meio falharia no
    primeiro CREATE TABLE do baseline — e o erro ("relation already exists")
    não diz o que fazer.
    """
    with engine.connect() as conexao:
        conexao.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        conexao.exec_driver_sql("CREATE SCHEMA public")
        # O pg_dump não traz o CREATE EXTENSION (a extensão não é objeto do
        # schema public), mas `ai_product_embeddings.embedding` é
        # `vector(1536)` e o arquivo não carrega sem o tipo existir.
        conexao.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        conexao.commit()


def aplicar_schema_baseline(engine: Engine) -> None:
    """Roda o arquivo inteiro numa tacada, pelo cursor cru do psycopg.

    Pelo SQLAlchemy não dá: qualquer caminho dele passa parâmetros para o
    driver, e aí o psycopg lê os `%` do corpo da função `rls_auto_enable`
    (`RAISE LOG '... on %'`) como placeholder e recusa o arquivo. Sem
    parâmetros, `cursor.execute` não procura placeholder nenhum.
    """
    sql = SCHEMA_BASELINE.read_text(encoding="utf-8")
    conexao = engine.raw_connection()
    try:
        conexao.cursor().execute(sql)
        conexao.commit()
    finally:
        conexao.close()

    # O arquivo abre com meia dúzia de `SET` de SESSÃO (`search_path` vazio,
    # `row_security = off`, `statement_timeout = 0`). Devolvida ao pool, essa
    # conexão entregaria tudo isso ao primeiro teste — e o sintoma é
    # "relation 'restaurants' does not exist" num banco em que a tabela
    # existe. Descartar o pool é mais seguro que desfazer SET por SET.
    engine.dispose()


def aplicar_migracoes(url: str) -> None:
    """Marca a foto como aplicada e roda o que vier depois dela.

    Em subprocesso porque `alembic/env.py` lê `settings.DATABASE_URL`, e
    `settings` já foi construído (a partir do `.env`) quando o pytest importou
    o primeiro módulo de `src`. Variável de ambiente de um processo novo é o
    caminho limpo de apontar o Alembic para outro banco; mexer no `settings` do
    processo em execução alcançaria também o código sob teste.
    """
    ambiente = {**os.environ, "DATABASE_URL": url}
    _rodar_alembic(["stamp", REVISAO_DO_SCHEMA_BASELINE], ambiente)
    _rodar_alembic(["upgrade", "head"], ambiente)


def _rodar_alembic(argumentos: list[str], ambiente: dict[str, str]) -> None:
    resultado = subprocess.run(
        [sys.executable, "-m", "alembic", *argumentos],
        cwd=RAIZ_DO_REPOSITORIO,
        env=ambiente,
        capture_output=True,
        text=True,
    )
    if resultado.returncode != 0:
        raise RuntimeError(
            f"`alembic {' '.join(argumentos)}` falhou:\n{resultado.stdout}\n{resultado.stderr}"
        )


@pytest.fixture(scope="session")
def engine_de_teste() -> Iterator[Engine]:
    """Banco com o schema pronto, uma vez por execução do pytest.

    Escopo de sessão porque montar o schema custa alguns segundos e nenhum
    teste tem motivo para vê-lo diferente: o isolamento entre testes é da
    fixture `db`, por transação.
    """
    url = url_do_banco_de_teste()
    recusar_banco_que_nao_e_de_teste(url)

    engine = create_engine(url)
    try:
        apagar_e_recriar_o_schema(engine)
    except Exception as erro:
        engine.dispose()
        pytest.fail(f"Não consegui falar com o Postgres de teste ({url}): {erro}\n\n{COMO_SUBIR_O_BANCO}")

    aplicar_schema_baseline(engine)
    aplicar_migracoes(url)

    yield engine
    engine.dispose()


@pytest.fixture
def db(engine_de_teste: Engine) -> Iterator[Session]:
    """Sessão que não deixa rastro: tudo o que o teste escrever é revertido.

    A transação é aberta na CONEXÃO, e a sessão entra nela com
    `join_transaction_mode="create_savepoint"` — assim um `commit()` do código
    sob teste solta um savepoint em vez de gravar de verdade, e o rollback lá
    embaixo continua levando tudo. É o que permite testar service (que commita)
    e repositório (que não) com a mesma fixture.

    O que NÃO volta é sequence: `nextval` é imune a rollback, de propósito. Um
    teste que depender do valor exato de `order_number` está errado pelo mesmo
    motivo que a armadilha 19 dá — a sequence é global.
    """
    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    sessao = Session(bind=conexao, autoflush=False, join_transaction_mode="create_savepoint")
    try:
        yield sessao
    finally:
        sessao.close()
        transacao.rollback()
        conexao.close()
