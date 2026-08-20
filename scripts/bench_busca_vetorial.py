"""Mede a busca vetorial do Rapi com e sem indice ANN (ivfflat, hnsw).

Existe para que o numero de `docs/busca-vetorial-e-indice-ann.md` seja
CONFERIVEL. A decisao registrada la — nao criar o indice — vale para o
tamanho de cardapio de hoje, e o unico jeito honesto de saber quando ela
deixar de valer e medir de novo.

    python scripts/bench_busca_vetorial.py                       # 161 produtos, 1 restaurante
    python scripts/bench_busca_vetorial.py --produtos 5000
    python scripts/bench_busca_vetorial.py --produtos 161 --restaurantes 30

O banco e DESCARTAVEL e criado por este script (`pedeaqui_bench_teste`, no
Postgres de `docker-compose.test.yml`). Ele nao toca em producao e nao le
`DATABASE_URL`: o endereco esta fixo aqui embaixo, apontando para localhost.

    docker compose -f docker-compose.test.yml up -d

O schema sai do mesmo caminho da suite `db` — `alembic/schema_baseline.sql`,
`stamp`, `upgrade head` (ver `tests/conftest.py` para por que nao e
`create_all`). Medir contra um schema montado de outro jeito seria medir
outro banco.

## O que e medido, e por que sao dois numeros

- **servidor**: o `Execution Time` do `EXPLAIN ANALYZE`. Responde "o indice
  ajuda?".
- **ida e volta**: o que o Rapi de fato espera. Responde "e isso ainda
  importa depois da rede?" — e a resposta hoje e nao: o vetor de 1536
  dimensoes viaja como ~20 KB de texto em TODA consulta, e esse custo
  fixo e maior que a varredura inteira.

Os vetores sao sinteticos, agrupados em 12 "temas" — embedding de cardapio
real nao e ruido uniforme, e sem os grupos a similaridade daria ~0 para tudo
e o piso de `AI_SEARCH_MIN_SIMILARITY` esvaziaria toda consulta. O que se
mede aqui e o CUSTO DA VARREDURA, que depende do numero de linhas e da
dimensao, nao do conteudo dos vetores.
"""

import argparse
import math
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import Engine, create_engine, text


RAIZ = Path(__file__).resolve().parent.parent
SCHEMA_BASELINE = RAIZ / "alembic" / "schema_baseline.sql"
REVISAO_DO_SCHEMA_BASELINE = "20260810_0012"

HOST = "postgresql+psycopg://pedeaqui:pedeaqui@localhost:55432"
BANCO = "pedeaqui_bench_teste"
URL = f"{HOST}/{BANCO}"

DIMENSOES = 1536
TOP_K = 5
MIN_SIMILARITY = 0.30
TEMAS = 12

# A consulta e copiada de `AIRepository.similarity_search`. Copia e nao
# import de proposito: o repositorio devolve linhas por uma Session do
# SQLAlchemy, e o que se quer cronometrar aqui e o SQL, sem a camada em cima.
# Se aquela consulta mudar, esta precisa mudar junto — e o numero do
# documento precisa ser refeito, que e justamente o ponto.
CONSULTA = """
    SELECT
        p.id, p.restaurant_id, p.name, p.slug, p.description, p.price,
        p.image_path, ape.metadata AS metadata,
        1 - (ape.embedding <=> CAST(:embedding AS vector)) AS similarity
    FROM ai_product_embeddings ape
    JOIN products p ON p.id = ape.product_id
    WHERE
        ape.restaurant_id = :restaurant_id
        AND p.restaurant_id = :restaurant_id
        AND p.branch_id = :branch_id
        AND p.is_active IS TRUE
        AND p.is_available IS TRUE
        AND (CAST(:max_price AS numeric) IS NULL OR p.price <= CAST(:max_price AS numeric))
        AND 1 - (ape.embedding <=> CAST(:embedding AS vector)) >= :min_similarity
    ORDER BY ape.embedding <=> CAST(:embedding AS vector)
    LIMIT :top_k
"""


# ---------------------------------------------------------------------------
# Vetores
# ---------------------------------------------------------------------------


def normalizar(vetor: list[float]) -> list[float]:
    norma = math.sqrt(sum(x * x for x in vetor)) or 1.0
    return [x / norma for x in vetor]


def vetor_aleatorio() -> list[float]:
    return normalizar([random.gauss(0, 1) for _ in range(DIMENSOES)])


def perto_de(centro: list[float], desvio: float) -> list[float]:
    """Um vizinho do centro. `desvio` ~ 1/sqrt(1536) da similaridade ~0,7."""
    return normalizar([x + random.gauss(0, desvio) for x in centro])


def formatar(vetor: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vetor) + "]"


# ---------------------------------------------------------------------------
# O banco descartavel
# ---------------------------------------------------------------------------


def recriar_banco() -> None:
    admin = create_engine(f"{HOST}/postgres", isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conexao:
            conexao.exec_driver_sql(f"DROP DATABASE IF EXISTS {BANCO} WITH (FORCE)")
            conexao.exec_driver_sql(f"CREATE DATABASE {BANCO}")
    finally:
        admin.dispose()


def montar_schema() -> Engine:
    engine = create_engine(URL)
    with engine.connect() as conexao:
        conexao.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        conexao.commit()

    # Cursor cru: pelo SQLAlchemy o psycopg leria os `%` do corpo de
    # `rls_auto_enable` como placeholder. Mesma razao de `tests/conftest.py`.
    conexao_crua = engine.raw_connection()
    try:
        conexao_crua.cursor().execute(SCHEMA_BASELINE.read_text(encoding="utf-8"))
        conexao_crua.commit()
    finally:
        conexao_crua.close()
    engine.dispose()

    ambiente = {**os.environ, "DATABASE_URL": URL}
    _rodar_alembic(["stamp", REVISAO_DO_SCHEMA_BASELINE], ambiente)
    _rodar_alembic(["upgrade", "head"], ambiente)
    return create_engine(URL)


def _rodar_alembic(argumentos: list[str], ambiente: dict[str, str]) -> None:
    resultado = subprocess.run(
        [sys.executable, "-m", "alembic", *argumentos],
        cwd=RAIZ, env=ambiente, capture_output=True, text=True,
    )
    if resultado.returncode != 0:
        raise RuntimeError(f"`alembic {' '.join(argumentos)}` falhou:\n{resultado.stderr}")


def semear(engine: Engine, produtos: int, restaurantes: int) -> tuple[str, str, list]:
    """Cria os cardapios. Devolve o restaurante, a filial e os temas dela.

    Cada restaurante tem temas PROPRIOS: e o que faz a consulta de um deles
    nao encontrar vizinho nenhum nos outros, como num cardapio de verdade.

    UMA filial por restaurante, de proposito: o cardapio pende de filial
    desde a revisao 20260820_0026, e o que esta medicao dimensiona e o
    tamanho de UM cardapio. Duas lojas com o mesmo cardapio dobrariam a
    tabela sem mudar o recorte que a consulta faz.
    """
    alvo = None
    with engine.begin() as conexao:
        for numero in range(restaurantes):
            centros = [vetor_aleatorio() for _ in range(TEMAS)]
            restaurant_id, branch_id = _semear_um_restaurante(
                conexao, numero, produtos, centros
            )
            if numero == 0:
                alvo = (str(restaurant_id), str(branch_id), centros)

    with engine.connect() as conexao:
        conexao.exec_driver_sql("ANALYZE")
        conexao.commit()
    return alvo


def _semear_um_restaurante(conexao, numero: int, produtos: int, centros: list):
    restaurant_id = conexao.execute(text(
        "INSERT INTO restaurants (name, slug) VALUES (:n, :s) RETURNING id"
    ), {"n": f"Bench {numero}", "s": f"bench-{numero}"}).scalar()

    branch_id = conexao.execute(text(
        "INSERT INTO branches (restaurant_id, name, slug, address, neighborhood,"
        " city, state, is_main, is_active)"
        " VALUES (:r, 'Matriz', :s, 'Rua X', 'Centro', 'Fortaleza', 'CE', true, true)"
        " RETURNING id"
    ), {"r": restaurant_id, "s": f"matriz-{numero}"}).scalar()

    category_id = conexao.execute(text(
        "INSERT INTO categories (restaurant_id, branch_id, name, slug, sort_order)"
        " VALUES (:r, :b, 'Geral', :s, 1) RETURNING id"
    ), {"r": restaurant_id, "b": branch_id, "s": f"geral-{numero}"}).scalar()

    for indice in range(produtos):
        product_id = conexao.execute(text(
            "INSERT INTO products (restaurant_id, branch_id, category_id, name, slug,"
            " price, is_active, is_available)"
            " VALUES (:r, :b, :c, :n, :s, :p, true, true) RETURNING id"
        ), {"r": restaurant_id, "b": branch_id, "c": category_id,
            "n": f"Produto {indice}", "s": f"produto-{numero}-{indice}",
            "p": 10 + (indice % 90)}).scalar()

        conexao.execute(text(
            "INSERT INTO ai_product_embeddings (restaurant_id, product_id, content,"
            " content_hash, metadata, embedding, updated_at)"
            " VALUES (:r, :p, :ct, :h, '{}'::jsonb, CAST(:e AS vector), now())"
        ), {"r": restaurant_id, "p": product_id, "ct": f"Produto {indice}",
            "h": f"{numero}-{indice}", "e": formatar(perto_de(centros[indice % TEMAS], 0.02))})

    return restaurant_id, branch_id


# ---------------------------------------------------------------------------
# A medicao
# ---------------------------------------------------------------------------


def perguntas(centros: list, quantas: int) -> list[dict]:
    """As perguntas JA formatadas, FORA da regiao cronometrada.

    Montar a string de 1536 floats custa dezenas de milissegundos em Python.
    Cronometrar isso junto mede o cliente, nao o banco — e esconde por
    completo a diferenca que se foi buscar.
    """
    return [
        {
            "restaurant_id": None,
            "branch_id": None,
            "embedding": formatar(perto_de(random.choice(centros), 0.025)),
            "top_k": TOP_K, "max_price": None, "min_similarity": MIN_SIMILARITY,
        }
        for _ in range(quantas)
    ]


def medir(engine: Engine, restaurant_id: str, branch_id: str, lote: list[dict]) -> dict:
    """Ida e volta de cada consulta, mais o tempo do servidor e o plano.

    O `EXPLAIN ANALYZE` roda FORA do laco cronometrado: a instrumentacao
    dele cobra por no e por linha, e numa varredura de milhares de linhas
    isso infla o proprio numero que se quer medir.
    """
    with engine.connect() as conexao:
        for parametros in lote[:20]:
            conexao.execute(text(CONSULTA), {
                **parametros,
                "restaurant_id": restaurant_id,
                "branch_id": branch_id,
            }).fetchall()

        tempos, devolvidas = [], []
        for parametros in lote:
            parametros = {
                **parametros,
                "restaurant_id": restaurant_id,
                "branch_id": branch_id,
            }
            inicio = time.perf_counter()
            linhas = conexao.execute(text(CONSULTA), parametros).fetchall()
            tempos.append((time.perf_counter() - inicio) * 1000)
            devolvidas.append(len(linhas))

        plano = [linha[0] for linha in conexao.execute(
            text("EXPLAIN (ANALYZE) " + CONSULTA),
            {**lote[0], "restaurant_id": restaurant_id, "branch_id": branch_id},
        ).fetchall()]

    return {
        "mediana": statistics.median(tempos),
        "p95": sorted(tempos)[int(len(tempos) * 0.95)],
        "servidor": _execucao(plano),
        "linhas": statistics.mean(devolvidas),
        "minimo": min(devolvidas),
        "no": _no_da_varredura(plano),
    }


def _execucao(plano: list[str]) -> float:
    for linha in plano:
        if linha.startswith("Execution Time:"):
            return float(linha.split(":")[1].strip().split()[0])
    return float("nan")


def _no_da_varredura(plano: list[str]) -> str:
    """Como o Postgres chegou nas linhas de `ai_product_embeddings`.

    E a leitura que decide tudo: indice que o planejador nao escolhe nao
    acelera nada e mesmo assim e mantido em toda escrita.
    """
    for linha in plano:
        if "ai_product_embeddings" in linha and "Scan" in linha:
            return linha.strip().lstrip("->").strip().split("  (")[0]
    return "?"


def criar_indice(engine: Engine, ddl: str) -> float:
    with engine.connect() as conexao:
        conexao.exec_driver_sql("DROP INDEX IF EXISTS bench_ann")
        inicio = time.perf_counter()
        conexao.exec_driver_sql(ddl)
        construcao = time.perf_counter() - inicio
        conexao.exec_driver_sql("ANALYZE ai_product_embeddings")
        conexao.commit()
    return construcao


def imprimir(nome: str, resultado: dict, base: float | None) -> None:
    ganho = "" if base is None else f"   ganho {(base / resultado['mediana'] - 1) * 100:+6.1f}%"
    print(f"  {nome:<18} mediana {resultado['mediana']:6.2f} ms   p95 {resultado['p95']:6.2f} ms   "
          f"servidor {resultado['servidor']:6.2f} ms{ganho}")
    print(f"  {'':<18} linhas/consulta {resultado['linhas']:.2f} (minimo {resultado['minimo']})   "
          f"leitura: {resultado['no']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--produtos", type=int, default=161,
                        help="produtos por restaurante (161 = cardapio do Junior)")
    parser.add_argument("--restaurantes", type=int, default=1)
    parser.add_argument("--consultas", type=int, default=300)
    parser.add_argument("--semente", type=int, default=20260817)
    argumentos = parser.parse_args()

    random.seed(argumentos.semente)
    total = argumentos.produtos * argumentos.restaurantes
    print(f"\n{argumentos.produtos} produtos x {argumentos.restaurantes} restaurante(s) "
          f"= {total} linhas em ai_product_embeddings")
    print(f"{argumentos.consultas} consultas por cenario, top_k={TOP_K}, "
          f"min_similarity={MIN_SIMILARITY}\n")

    recriar_banco()
    engine = montar_schema()
    restaurant_id, branch_id, centros = semear(
        engine, argumentos.produtos, argumentos.restaurantes
    )
    lote = perguntas(centros, argumentos.consultas)

    sem_indice = medir(engine, restaurant_id, branch_id, lote)
    print("SEM INDICE (o que roda hoje)")
    imprimir("sem indice", sem_indice, None)

    lists = max(1, total // 100 if total >= 1000 else int(math.sqrt(total)))
    cenarios = (
        (f"ivfflat lists={lists}",
         "CREATE INDEX bench_ann ON ai_product_embeddings USING ivfflat"
         f" (embedding vector_cosine_ops) WITH (lists = {lists})"),
        ("hnsw m=16",
         "CREATE INDEX bench_ann ON ai_product_embeddings USING hnsw"
         " (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"),
    )
    for nome, ddl in cenarios:
        construcao = criar_indice(engine, ddl)
        print(f"\nCOM {nome.upper()}  (construcao: {construcao:.2f} s)")
        imprimir(nome, medir(engine, restaurant_id, branch_id, lote), sem_indice["mediana"])

    with engine.connect() as conexao:
        conexao.exec_driver_sql("DROP INDEX IF EXISTS bench_ann")
        conexao.commit()
    engine.dispose()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
