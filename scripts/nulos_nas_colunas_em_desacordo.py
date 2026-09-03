"""Quantas linhas nulas existem hoje nas colunas que o ORM jura NOT NULL.

E a CONFERENCIA QUE ANTECEDE o alinhamento do schema. `ALTER TABLE ... SET NOT
NULL` varre a tabela inteira e falha se achar uma linha nula — e falhar no meio
de uma migracao contra producao, com a API fora do ar, e a pior hora possivel
para descobrir que ha dado a decidir. Este script responde antes, e sem tocar
em nada: so `SELECT count(*)`.

A lista de colunas NAO esta escrita aqui. Ela sai de
`divergencias_orm_schema.comparar()`, a mesma funcao que o outro script usa —
uma segunda copia envelheceria sozinha, e o dia em que as duas discordassem
seria o dia em que uma migracao alinharia a coluna errada.

    python scripts/nulos_nas_colunas_em_desacordo.py \\
        --url postgresql+psycopg://pedeaqui:pedeaqui@localhost:55432/pedeaqui_teste

Como ler a saida, coluna por coluna:

    0 nulo(s)   a coluna esta PRONTA para `SET NOT NULL`. O roteiro em
                `docs/alinhamento-orm-schema.md` vale sem mudanca.
    N nulo(s)   NAO alinhe ainda. Antes existe uma decisao de DADO — o que
                aquelas N linhas deveriam dizer — e ela nao e minha nem do
                Alembic. Preencha, apague ou tire a coluna da revisao.

O numero envelhece: entre esta leitura e a migracao, linha nova pode entrar.
Por isso a etapa 2 do roteiro reconfere com `VALIDATE CONSTRAINT`, que e a
unica leitura que vale — a que acontece dentro da propria transacao.

Somente leitura. Roda contra o banco de teste ou contra producao (`docker exec
pedeaqui-api python scripts/nulos_nas_colunas_em_desacordo.py`); nao escreve,
nao cria indice, nao tranca tabela — `count(*)` toma apenas `ACCESS SHARE`.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, inspect, text

from scripts.divergencias_orm_schema import comparar
from src.db.session import get_engine


# O identificador entra na consulta por interpolacao — nao ha como parametrizar
# nome de coluna em SQL. Ele vem do `inspect()` do proprio banco e nao de
# entrada de usuario, mas a guarda fica porque o custo dela e uma linha e o
# custo de nao te-la e uma injecao no dia em que alguem passar a alimentar a
# lista de outro lugar.
IDENTIFICADOR_VALIDO = re.compile(r"^[a-z_][a-z0-9_]*$")


def contar_nulos(engine, colunas: list[tuple[str, str]], schema: str) -> list[tuple[str, str, int, int]]:
    """Para cada (tabela, coluna): quantos nulos, de quantas linhas.

    Uma consulta por TABELA e nao por coluna: `count(*) FILTER (WHERE ...)`
    resolve as colunas todas numa varredura so, e tabela grande varrida seis
    vezes seria seis vezes o custo pelo mesmo numero.
    """
    por_tabela: dict[str, list[str]] = {}
    for tabela, coluna in colunas:
        por_tabela.setdefault(tabela, []).append(coluna)

    resultados: list[tuple[str, str, int, int]] = []
    with engine.connect() as conexao:
        for tabela, nomes in por_tabela.items():
            if not IDENTIFICADOR_VALIDO.match(tabela) or not all(
                IDENTIFICADOR_VALIDO.match(nome) for nome in nomes
            ):
                raise ValueError(f"identificador fora do esperado em {tabela}: {nomes}")
            filtros = ", ".join(
                f'count(*) FILTER (WHERE "{nome}" IS NULL) AS "{nome}"' for nome in nomes
            )
            linha = conexao.execute(
                text(f'SELECT {filtros}, count(*) AS total FROM "{schema}"."{tabela}"')
            ).mappings().one()
            for nome in nomes:
                resultados.append((tabela, nome, int(linha[nome]), int(linha["total"])))
    return resultados


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Conta nulos nas colunas que o ORM declara NOT NULL (somente leitura)."
    )
    parser.add_argument("--url", help="URL do banco. Sem ela, usa a DATABASE_URL do ambiente.")
    parser.add_argument("--schema", default="public", help="Schema a inspecionar (padrao: public).")
    args = parser.parse_args()

    engine = create_engine(args.url) if args.url else get_engine()
    inspetor = inspect(engine)

    if not inspetor.get_table_names(schema=args.schema):
        print(f"Nenhuma tabela no schema '{args.schema}'. A URL aponta para o banco certo?")
        return 1

    colunas = comparar(inspetor, args.schema).orm_mais_estrito
    if not colunas:
        print("Nenhuma coluna em que o ORM diga NOT NULL e o banco aceite NULL.")
        print("Ou o schema ja foi alinhado, ou a URL aponta para outro banco.")
        return 0

    resultados = contar_nulos(engine, colunas, args.schema)
    com_nulo = [linha for linha in resultados if linha[2] > 0]

    print("=" * 72)
    print(f"{len(colunas)} coluna(s) em desacordo  |  {len(com_nulo)} com linha nula hoje")
    print("=" * 72)
    print()

    largura = max(len(f"{tabela}.{coluna}") for tabela, coluna, _, _ in resultados)
    for tabela, coluna, nulos, total in sorted(resultados):
        veredito = "PRONTA para SET NOT NULL" if nulos == 0 else "DECIDIR O DADO ANTES"
        print(
            f"  {f'{tabela}.{coluna}':<{largura}}  "
            f"{nulos:>8} nulo(s) de {total:>8} linha(s)   {veredito}"
        )

    print()
    if com_nulo:
        print("Ha coluna com linha nula. NAO rode a etapa 2 do alinhamento sobre ela:")
        print("o `VALIDATE CONSTRAINT` recusaria, e a transacao inteira voltaria.")
        print("O roteiro esta em docs/alinhamento-orm-schema.md.")
        return 2

    print("Nenhuma linha nula. As colunas acima aceitam o alinhamento como esta")
    print("escrito em docs/alinhamento-orm-schema.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
