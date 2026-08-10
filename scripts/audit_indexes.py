"""Procura indices duplicados e fora da convencao no banco.

Escrito depois de a 20260806_0010 quebrar em producao e de a 20260810_0012
quase criar `ix_order_items_order_id` ao lado de um
`idx_order_items_order_id` que ja existia. Sao dois problemas diferentes, e
o segundo e o pior:

1. **Nome ocupado.** Uma revisao tenta criar um indice cujo nome ja existe e
   a migracao morre. Barulhento, e a convencao do `if_not_exists=True`
   (20260806_0010) ja resolve.

2. **Definicao duplicada.** Dois indices com nomes DIFERENTES sobre a mesma
   tabela e as mesmas colunas. `IF NOT EXISTS` casa por nome e nao enxerga
   isto: a migracao passa, ninguem ve nada, e o banco fica mantendo duas
   arvores identicas em toda escrita — custo de INSERT dobrado numa consulta
   a mais de zero. E o modo de falha SILENCIOSO, e e o que este script
   existe para achar.

Read-only: nao executa DDL. Para as duplicatas ele imprime o `op.drop_index`
sugerido, para ser colado numa revisao — a decisao de qual dos dois nomes
fica e sempre humana.

Uso:

    python scripts/audit_indexes.py
    python scripts/audit_indexes.py --all-schemas
    docker exec pedeaqui-api python scripts/audit_indexes.py
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text

from src.db.session import SessionLocal


VERSIONS_DIR = ROOT_DIR / "alembic" / "versions"

# Prefixos que a convencao do projeto usa. `ix_` para indice comum, `uq_`
# para restricao de unicidade. Indice de PRIMARY KEY / UNIQUE constraint nao
# entra na conferencia: quem nomeia aquilo e a restricao, nao nos.
CONVENTIONAL_PREFIXES = ("ix_", "uq_")

# Primeiro argumento de op.create_index("nome", ...) / op.create_table("nome").
# Heuristica de texto, nao analise sintatica: pega o caso normal (literal na
# chamada) e perde nome montado em variavel. Serve para orientar a leitura,
# nao para decidir sozinho.
_CREATE_INDEX = re.compile(r"""op\.create_index\(\s*["']([^"']+)["']""")
_CREATE_TABLE = re.compile(r"""op\.create_table\(\s*["']([^"']+)["']""")

INDEX_QUERY = """
SELECT
    n.nspname                          AS schema_name,
    t.relname                          AS table_name,
    i.relname                          AS index_name,
    idx.indisprimary                   AS is_primary,
    idx.indisunique                    AS is_unique,
    con.contype                        AS constraint_type,
    pg_get_indexdef(idx.indexrelid)    AS definition,
    pg_relation_size(idx.indexrelid)   AS size_bytes
FROM pg_index idx
JOIN pg_class i      ON i.oid = idx.indexrelid
JOIN pg_class t      ON t.oid = idx.indrelid
JOIN pg_namespace n  ON n.oid = t.relnamespace
LEFT JOIN pg_constraint con ON con.conindid = idx.indexrelid
WHERE t.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND (:only_public IS FALSE OR n.nspname = 'public')
ORDER BY n.nspname, t.relname, i.relname
"""


def normalize_definition(definition: str, index_name: str) -> str:
    """A definicao do indice sem o nome dele.

    E o que permite dizer que `idx_order_items_order_id` e
    `ix_order_items_order_id` sao o MESMO indice. Sai de
    `pg_get_indexdef`, entao ja vem normalizada pelo proprio Postgres:
    UNIQUE, opclass, ordem, `WHERE` de indice parcial e expressao de indice
    funcional estao todos ali. Comparar a lista de colunas na mao perderia
    justamente esses casos.
    """
    return definition.replace(f" {index_name} ", " <name> ", 1)


def migration_declared_names() -> tuple[set[str], set[str]]:
    """O que as revisoes do Alembic dizem criar: (indices, tabelas)."""
    indexes: set[str] = set()
    tables: set[str] = set()
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        indexes.update(_CREATE_INDEX.findall(source))
        tables.update(_CREATE_TABLE.findall(source))
    return indexes, tables


def pick_survivor(found: list[dict], declared_indexes: set[str]) -> dict:
    """Qual dos indices duplicados deveria ficar.

    A convencao de nome pesa MAIS que "declarado por uma revisao", e a ordem
    importa de verdade: o nome legado pode estar declarado tambem, quando
    alguma revisao o recria no downgrade — e o caso da 20260810_0012, que
    recria `idx_order_items_order_id` para nao deixar a tabela sem indice se
    o deploy voltar atras. Uma regra que olhasse "declarado" primeiro
    escolheria justamente o nome legado ali, o contrario do que a adocao
    quis fazer.

    Desempate final pelo nome so para a saida ser estavel entre execucoes.
    """
    return min(
        found,
        key=lambda row: (
            not row["index_name"].startswith(CONVENTIONAL_PREFIXES),
            row["index_name"] not in declared_indexes,
            row["index_name"],
        ),
    )


def survivor_reason(keep: dict, declared_indexes: set[str]) -> str:
    reasons = []
    if keep["index_name"].startswith(CONVENTIONAL_PREFIXES):
        reasons.append("nome da convencao")
    if keep["index_name"] in declared_indexes:
        reasons.append("declarado por uma revisao")
    return f"  ({', '.join(reasons)})" if reasons else ""


def human_size(size_bytes: int | None) -> str:
    if not size_bytes:
        return "?"
    size = float(size_bytes)
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.0f} GB"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Procura indices duplicados e fora da convencao (somente leitura)."
    )
    parser.add_argument(
        "--all-schemas",
        action="store_true",
        help="Inclui schemas alem de public.",
    )
    args = parser.parse_args()

    declared_indexes, migration_tables = migration_declared_names()

    with SessionLocal() as db:
        rows = db.execute(
            text(INDEX_QUERY), {"only_public": not args.all_schemas}
        ).mappings().all()

    if not rows:
        print("Nenhum indice encontrado. A conexao aponta para o banco certo?")
        return 1

    by_definition: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    off_convention: list[dict] = []
    unowned: list[dict] = []

    for row in rows:
        key = (
            row["schema_name"],
            row["table_name"],
            normalize_definition(row["definition"], row["index_name"]),
        )
        by_definition[key].append(row)

        # Indice de PK ou de restricao e nomeado pela restricao; a convencao
        # de nome do projeto nao se aplica a ele.
        if row["is_primary"] or row["constraint_type"] is not None:
            continue
        if not row["index_name"].startswith(CONVENTIONAL_PREFIXES):
            off_convention.append(row)
        # Tabela do baseline = tabela que nenhuma revisao criou. Indice ali
        # que nenhuma revisao declara e objeto que so existe no banco: e
        # exatamente o que uma revisao futura pode colidir sem saber.
        if (
            row["table_name"] not in migration_tables
            and row["index_name"] not in declared_indexes
        ):
            unowned.append(row)

    duplicates = {key: found for key, found in by_definition.items() if len(found) > 1}

    print("=" * 72)
    print(f"{len(rows)} indice(s) lidos")
    print("=" * 72)

    print()
    print(f"## Definicao duplicada  ({len(duplicates)})")
    print()
    if not duplicates:
        print("  Nenhuma. Nenhum par de indices cobre exatamente a mesma coisa.")
    else:
        print("  Dois indices sobre a MESMA definicao. O banco mantem os dois em")
        print("  toda escrita. IF NOT EXISTS nao pega isto: ele casa por nome.")
        print()
        for (schema_name, table_name, _), found in sorted(duplicates.items()):
            names = ", ".join(
                f"{row['index_name']} ({human_size(row['size_bytes'])})" for row in found
            )
            print(f"  {schema_name}.{table_name}: {names}")
            print(f"    {found[0]['definition']}")
            keep = pick_survivor(found, declared_indexes)
            print(f"    manter: {keep['index_name']}{survivor_reason(keep, declared_indexes)}")
            for row in found:
                if row["index_name"] == keep["index_name"]:
                    continue
                print(
                    f'    op.drop_index("{row["index_name"]}", '
                    f'table_name="{row["table_name"]}", if_exists=True)'
                )
            print()

    print()
    print(f"## Nome fora da convencao  ({len(off_convention)})")
    print()
    if not off_convention:
        print(f"  Nenhum. Todos comecam com {' ou '.join(CONVENTIONAL_PREFIXES)}.")
    else:
        print("  Sozinho isto nao quebra nada: so vira armadilha quando uma")
        print("  revisao futura criar o equivalente com o nome da convencao.")
        print()
        for row in off_convention:
            print(f"  {row['schema_name']}.{row['table_name']}.{row['index_name']}")
            print(f"    {row['definition']}")

    print()
    print(f"## Sem dono no Alembic  ({len(unowned)})")
    print()
    if not unowned:
        print("  Nenhum. Todo indice de tabela do baseline esta declarado.")
    else:
        print("  Indice em tabela do baseline que nenhuma revisao declara. Existe")
        print("  so no banco: um `alembic downgrade` nao o remove e um")
        print("  `--autogenerate` propoe derruba-lo. Nao e erro; e a lista do que")
        print("  ainda nao foi adotado.")
        print()
        for row in unowned:
            print(f"  {row['schema_name']}.{row['table_name']}.{row['index_name']}")

    print()
    return 1 if duplicates else 0


if __name__ == "__main__":
    raise SystemExit(main())
