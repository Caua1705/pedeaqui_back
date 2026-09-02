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
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine.reflection import Inspector

# `import src.models` e o que popula o `Base.metadata`: o `__init__` importa os
# 34 modulos, e tabela cujo modulo nao esteja la simplesmente nao existe para o
# metadata (foi o que aconteceu com `delivery_estimates`, e esta anotado la).
import src.models  # noqa: F401
from src.db.base import Base
from src.db.session import get_engine


# Tabela de controle do Alembic. Ela existe no banco de proposito e nao tem
# model — nao e divergencia, e listada como uma seria ruido em toda execucao.
TABELAS_SEM_MODEL_ESPERADAS = {"alembic_version"}


@dataclass(frozen=True)
class Divergencias:
    """As tres classes, ja separadas. Uma execucao da comparacao, um objeto.

    Existe para haver UM lugar que decide o que e divergencia. Quem tambem
    precisa da primeira lista e `scripts/nulos_nas_colunas_em_desacordo.py`
    (a conferencia que antecede o `SET NOT NULL`), e uma segunda copia da
    regra ali seria a divergencia entre os dois scripts esperando acontecer —
    justamente o defeito que este arquivo existe para nomear.
    """

    orm_mais_estrito: list[tuple[str, str]]
    banco_mais_estrito: list[tuple[str, str, str | None]]
    nao_mapeadas: list[tuple[str, str, bool, str | None]]
    so_no_banco: list[str]
    so_no_orm: list[str]
    tabelas_do_orm: int
    tabelas_do_banco: int

    @property
    def total(self) -> int:
        return len(self.orm_mais_estrito) + len(self.banco_mais_estrito) + len(self.nao_mapeadas)


def comparar(inspetor: Inspector, schema: str = "public") -> Divergencias:
    """Compara `Base.metadata` com o schema de um banco. Nao imprime nada."""
    tabelas_do_banco = set(inspetor.get_table_names(schema=schema))
    tabelas_do_orm = set(Base.metadata.tables)

    so_no_banco = sorted(tabelas_do_banco - tabelas_do_orm - TABELAS_SEM_MODEL_ESPERADAS)
    so_no_orm = sorted(tabelas_do_orm - tabelas_do_banco)

    orm_mais_estrito: list[tuple[str, str]] = []
    banco_mais_estrito: list[tuple[str, str, str | None]] = []
    nao_mapeadas: list[tuple[str, str, bool, str | None]] = []

    for nome_da_tabela in sorted(tabelas_do_orm & tabelas_do_banco):
        tabela = Base.metadata.tables[nome_da_tabela]
        colunas_do_banco = {
            coluna["name"]: coluna
            for coluna in inspetor.get_columns(nome_da_tabela, schema=schema)
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

    return Divergencias(
        orm_mais_estrito=orm_mais_estrito,
        banco_mais_estrito=banco_mais_estrito,
        nao_mapeadas=nao_mapeadas,
        so_no_banco=so_no_banco,
        so_no_orm=so_no_orm,
        tabelas_do_orm=len(tabelas_do_orm),
        tabelas_do_banco=len(tabelas_do_banco),
    )


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
    parser.add_argument(
        "--limite",
        type=int,
        help=(
            "Quantas divergencias sao esperadas hoje. Passar deste numero vira "
            "AVISO (anotacao do GitHub Actions), nunca falha. Ficar abaixo vira "
            "lembrete de baixar o limite."
        ),
    )
    args = parser.parse_args()

    engine = create_engine(args.url) if args.url else get_engine()
    inspetor = inspect(engine)

    if not inspetor.get_table_names(schema=args.schema):
        print(f"Nenhuma tabela no schema '{args.schema}'. A URL aponta para o banco certo?")
        return 1

    divergencias = comparar(inspetor, args.schema)
    so_no_banco = divergencias.so_no_banco
    so_no_orm = divergencias.so_no_orm
    orm_mais_estrito = divergencias.orm_mais_estrito
    banco_mais_estrito = divergencias.banco_mais_estrito
    nao_mapeadas = divergencias.nao_mapeadas

    print("=" * 72)
    print(
        f"{divergencias.tabelas_do_orm} tabela(s) no ORM, "
        f"{divergencias.tabelas_do_banco} no banco  |  "
        f"{divergencias.total} divergencia(s) de coluna"
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

    if args.limite is not None:
        avisar_sobre_o_limite(divergencias.total, args.limite)
    return 0


def avisar_sobre_o_limite(total: int, limite: int) -> None:
    """Compara o total com o esperado e escreve UM aviso. Nunca falha.

    POR QUE AVISO E NAO FALHA. As 42 divergencias de hoje sao herdadas: o
    schema e mais velho que o ORM e nenhuma delas foi introduzida por um
    commit. Um portao vermelho contra divida herdada e um portao que se
    aprende a ignorar — e o dia em que ele acusar uma divergencia NOVA sera o
    dia em que alguem o desligar para conseguir entregar.

    O aviso resolve a coisa especifica que faltava: o numero nao cresce mais
    calado. Passou de 42, aparece na aba de Summary do Actions e no diff da PR,
    e quem escreveu a coluna ve na hora.

    `::warning::` e `::notice::` sao as anotacoes do GitHub Actions. Fora do
    Actions os prefixos sao ruido inofensivo, e o texto depois deles continua
    legivel — nao ha ramo separado para "estou no CI".
    """
    if total > limite:
        print()
        print(
            f"::warning title=Divergencias ORM x schema::"
            f"{total} divergencia(s) de coluna, e o esperado era {limite}. "
            f"{total - limite} nova(s). "
            "Ver docs/alinhamento-orm-schema.md e a saida acima para saber qual. "
            "Se a divergencia nova for deliberada, suba o --limite no ci.yml no "
            "mesmo commit que a criou — o que este aviso nao aceita e ela entrar "
            "sem ninguem ver."
        )
    elif total < limite:
        print()
        print(
            f"::notice title=Divergencias ORM x schema::"
            f"{total} divergencia(s), abaixo do limite de {limite}. "
            "Baixe o --limite no ci.yml para travar o ganho — limite folgado "
            "deixa a proxima divergencia entrar de graca."
        )


if __name__ == "__main__":
    raise SystemExit(main())
