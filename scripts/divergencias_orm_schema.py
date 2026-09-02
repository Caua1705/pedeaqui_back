"""Compara o que o ORM declara com o que o banco realmente tem.

O schema deste projeto nao nasceu do ORM: ele foi criado a mao no Supabase e
so depois virou `alembic/schema_baseline.sql`. O `Base.metadata` foi escrito
DEPOIS, olhando para as tabelas que ja existiam — e, em 42 colunas, o que ele
declara nao e o que o banco faz cumprir.

Nenhuma dessas divergencias aparece em teste, e o motivo e que nada as
confere: `Base.metadata.create_all()` nao e usado (armadilha 24, e o
docstring de `tests/conftest.py`), entao o `nullable=` do model nunca vira
DDL. Ele e uma anotacao que o SQLAlchemy usa para o type checker e para
decidir se manda a coluna no INSERT — e mais nada.

As tres classes que este script separa, porque custam coisas diferentes:

1. **ORM diz NOT NULL, banco aceita NULL.** Risco de LEITURA. O model promete
   `Mapped[str]` e o banco pode entregar `None`; quem escrever
   `cliente.email.lower()` confiando na anotacao leva `AttributeError` numa
   linha antiga, nao no teste.

2. **Banco diz NOT NULL, ORM diz nullable.** Risco de ESCRITA — mas so onde
   NAO ha `DEFAULT`. Com `DEFAULT now()` ou `DEFAULT 0`, omitir a coluna no
   INSERT e seguro e a anotacao apenas mente para quem le. Sem default, um
   `Modelo(...)` que esqueca a coluna estoura `IntegrityError` em runtime,
   com o type checker satisfeito. Por isso a saida imprime o default: e ele
   quem separa o benigno do que morde.

3. **Coluna que o ORM nao mapeia.** Invisivel: `SELECT *` do ORM nao a traz,
   e `Modelo.created_at` e `AttributeError` em tabela que TEM `created_at`.

Somente leitura: nao executa DDL e nao escreve nada. Contra o banco de teste
(o schema que o repositorio constroi) ou contra producao:

    python scripts/divergencias_orm_schema.py \\
        --url postgresql+psycopg://pedeaqui:pedeaqui@localhost:55432/pedeaqui_teste
    docker exec pedeaqui-api python scripts/divergencias_orm_schema.py

Sem `--url` ele usa a `DATABASE_URL` do ambiente, como qualquer script daqui.
"""

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, inspect

# `import src.models` e o que popula o `Base.metadata`: o `__init__` importa os
# 34 modulos, e tabela cujo modulo nao esteja la simplesmente nao existe para o
# metadata (foi o que aconteceu com `delivery_estimates`, e esta anotado la).
import src.models  # noqa: F401
from src.db.base import Base
from src.db.session import get_engine


# Tabela de controle do Alembic. Ela existe no banco de proposito e nao tem
# model — nao e divergencia, e listada como uma seria ruido em toda execucao.
TABELAS_SEM_MODEL_ESPERADAS = {"alembic_version"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compara Base.metadata com o schema do banco (somente leitura)."
    )
    parser.add_argument(
        "--url",
        help="URL do banco a conferir. Sem ela, usa a DATABASE_URL do ambiente.",
    )
    parser.add_argument(
        "--schema",
        default="public",
        help="Schema a inspecionar (padrao: public).",
    )
    args = parser.parse_args()

    engine = create_engine(args.url) if args.url else get_engine()
    inspetor = inspect(engine)

    tabelas_do_banco = set(inspetor.get_table_names(schema=args.schema))
    tabelas_do_orm = set(Base.metadata.tables)

    if not tabelas_do_banco:
        print(f"Nenhuma tabela no schema '{args.schema}'. A URL aponta para o banco certo?")
        return 1

    so_no_banco = sorted(tabelas_do_banco - tabelas_do_orm - TABELAS_SEM_MODEL_ESPERADAS)
    so_no_orm = sorted(tabelas_do_orm - tabelas_do_banco)

    orm_mais_estrito: list[tuple[str, str]] = []
    banco_mais_estrito: list[tuple[str, str, str | None]] = []
    nao_mapeadas: list[tuple[str, str, bool, str | None]] = []

    for nome_da_tabela in sorted(tabelas_do_orm & tabelas_do_banco):
        tabela = Base.metadata.tables[nome_da_tabela]
        colunas_do_banco = {
            coluna["name"]: coluna
            for coluna in inspetor.get_columns(nome_da_tabela, schema=args.schema)
        }

        for coluna in tabela.columns:
            no_banco = colunas_do_banco.get(coluna.name)
            if no_banco is None:
                # Coluna que so o ORM conhece. O SELECT dele a pede, e o banco
                # responde erro — nao e sutil, e por isso nao ganha secao.
                so_no_orm.append(f"{nome_da_tabela}.{coluna.name} (coluna)")
                continue
            if bool(coluna.nullable) == bool(no_banco["nullable"]):
                continue
            if coluna.nullable:
                banco_mais_estrito.append(
                    (nome_da_tabela, coluna.name, no_banco.get("default"))
                )
            else:
                orm_mais_estrito.append((nome_da_tabela, coluna.name))

        for nome_da_coluna, no_banco in colunas_do_banco.items():
            if nome_da_coluna not in tabela.columns:
                nao_mapeadas.append(
                    (
                        nome_da_tabela,
                        nome_da_coluna,
                        bool(no_banco["nullable"]),
                        no_banco.get("default"),
                    )
                )

    total = len(orm_mais_estrito) + len(banco_mais_estrito) + len(nao_mapeadas)

    print("=" * 72)
    print(
        f"{len(tabelas_do_orm)} tabela(s) no ORM, "
        f"{len(tabelas_do_banco)} no banco  |  {total} divergencia(s) de coluna"
    )
    print("=" * 72)

    if so_no_banco or so_no_orm:
        print()
        print("## Tabela que existe de um lado so")
        print()
        for nome in so_no_banco:
            print(f"  so no banco: {nome}")
        for nome in so_no_orm:
            print(f"  so no ORM  : {nome}")

    print()
    print(f"## ORM diz NOT NULL, banco aceita NULL  ({len(orm_mais_estrito)})")
    print()
    if not orm_mais_estrito:
        print("  Nenhuma.")
    else:
        print("  Risco de leitura: a anotacao promete valor e o banco pode dar None.")
        print()
        for nome_da_tabela, nome_da_coluna in orm_mais_estrito:
            print(f"  {nome_da_tabela}.{nome_da_coluna}")

    print()
    print(f"## Banco diz NOT NULL, ORM diz nullable  ({len(banco_mais_estrito)})")
    print()
    if not banco_mais_estrito:
        print("  Nenhuma.")
    else:
        print("  Com DEFAULT, omitir no INSERT e seguro e a anotacao so engana quem le.")
        print("  SEM default (marcadas), um Modelo(...) que esqueca a coluna estoura.")
        print()
        for nome_da_tabela, nome_da_coluna, padrao in banco_mais_estrito:
            marca = "  <-- SEM DEFAULT" if padrao is None else ""
            print(
                f"  {nome_da_tabela}.{nome_da_coluna}"
                f"  DEFAULT {padrao if padrao is not None else '(nenhum)'}{marca}"
            )

    print()
    print(f"## Coluna que o ORM nao mapeia  ({len(nao_mapeadas)})")
    print()
    if not nao_mapeadas:
        print("  Nenhuma.")
    else:
        print("  Existe no banco e o model nao a enxerga.")
        print()
        for nome_da_tabela, nome_da_coluna, aceita_nulo, padrao in nao_mapeadas:
            print(
                f"  {nome_da_tabela}.{nome_da_coluna}"
                f"  nullable={aceita_nulo}  DEFAULT {padrao if padrao is not None else '(nenhum)'}"
            )

    print()
    print("Nada aqui e erro por si so — o schema e mais velho que o ORM. O que")
    print("este script diz e ONDE a anotacao do model nao pode ser levada a serio.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
