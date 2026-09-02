"""Onde o codigo LE uma coluna que o banco deixa nula, em forma que quebra.

A armadilha 50 diz que 42 colunas discordam entre o `nullable=` do model e o
DDL do banco, e que 16 delas sao risco de LEITURA: a anotacao promete valor e o
banco pode dar `None`. O que ela NAO diz e onde, exatamente, esse `None`
chegaria.

Este script responde isso. Ele existe pela licao que custou a rodada anterior:
**ferramenta antes do conserto.** Consertando por grep, conserta-se o caso que
apareceu e nao a classe — e o proximo `AttributeError` vem do irmao que ficou.

## O que ele considera "forma que quebra"

Um acesso `objeto.coluna` cujo PAI na arvore sintatica faz uma destas coisas:

- **`.metodo()` ou `.atributo`** em cima — `valid_until.tzinfo` e
  `AttributeError` se for `None`;
- **comparacao de ordem** (`< <= > >=`) — `TypeError` entre `None` e datetime;
- **aritmetica** (`+ - * /`) — mesma coisa;
- **argumento de chamada**, nomeado ou posicional — e assim que um `None` entra
  num schema de resposta que declara o campo sem `| None`, e vira 500 na
  serializacao.

E ele considera PROTEGIDO quando o acesso esta claramente guardado: dentro de
`if objeto.coluna`, `objeto.coluna is None`, `objeto.coluna or X`,
`not objeto.coluna`, ou passado a `getattr(..., None)`.

## O que ele NAO consegue fazer, e e melhor dizer

**Ele casa por NOME de atributo, nao por tipo.** `email`, `number`,
`created_at` e `is_active` sao nomes de coluna de meia duzia de tabelas, e o
script nao sabe de qual delas veio o objeto. Por isso:

- a saida e POR COLUNA, e o julgamento continua sendo de quem le;
- `--coluna` filtra, e e como se usa para investigar uma;
- o numero que o portao vigia (`--limite`) e uma LINHA DE BASE, nao uma conta
  de defeitos. Ele responde "entrou leitura nova?", nao "ha N bugs".

A lista de colunas nao esta escrita aqui: sai de
`divergencias_orm_schema.comparar()`, contra um banco de verdade. Uma segunda
copia envelheceria sozinha.

    python scripts/leituras_de_coluna_nulavel.py \\
        --url postgresql+psycopg://pedeaqui:pedeaqui@localhost:55432/pedeaqui_teste
    python scripts/leituras_de_coluna_nulavel.py --url ... --coluna valid_until

Somente leitura: le o schema e o codigo-fonte, nao executa nada de `src/`.
"""

import argparse
import ast
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, inspect

from scripts.divergencias_orm_schema import comparar
from src.db.session import get_engine


DIRETORIOS = ("src", "scripts")

# Comparacao de ORDEM. `==` e `!=` ficam de fora: `None == x` e False, nao
# TypeError, e comparar por igualdade com nulo e um jeito legitimo (ainda que
# torto) de perguntar se ha valor.
COMPARACOES_DE_ORDEM = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


class _Visitante(ast.NodeVisitor):
    """Anota o pai de cada no, que e o que decide se a leitura quebra."""

    def __init__(self) -> None:
        self.pais: dict[ast.AST, ast.AST] = {}

    def generic_visit(self, no: ast.AST) -> None:
        for filho in ast.iter_child_nodes(no):
            self.pais[filho] = no
        super().generic_visit(no)


def _protegido(no: ast.Attribute, pais: dict[ast.AST, ast.AST]) -> bool:
    pai = pais.get(no)
    if isinstance(pai, ast.Compare) and any(
        isinstance(operador, (ast.Is, ast.IsNot)) for operador in pai.ops
    ):
        return True
    if isinstance(pai, ast.BoolOp):
        # `x.col or padrao` e `x.col and ...`: os dois so seguem com valor.
        return True
    if isinstance(pai, ast.UnaryOp) and isinstance(pai.op, ast.Not):
        return True
    if isinstance(pai, (ast.If, ast.IfExp, ast.While, ast.Assert)):
        return True
    if isinstance(pai, ast.Call) and isinstance(pai.func, ast.Name) and pai.func.id == "getattr":
        return True
    return False


def _forma_que_quebra(no: ast.Attribute, pais: dict[ast.AST, ast.AST]) -> str | None:
    pai = pais.get(no)
    if isinstance(pai, ast.Attribute):
        return "atributo/metodo em cima do valor"
    if isinstance(pai, ast.Compare) and any(
        isinstance(operador, COMPARACOES_DE_ORDEM) for operador in pai.ops
    ):
        return "comparacao de ordem"
    if isinstance(pai, ast.BinOp):
        return "aritmetica"
    if isinstance(pai, ast.keyword):
        return "argumento nomeado (schema de resposta?)"
    if isinstance(pai, ast.Call) and no in pai.args:
        return "argumento posicional"
    return None


def leituras(colunas: set[str]) -> list[tuple[str, int, str, str, str]]:
    achados = []
    for diretorio in DIRETORIOS:
        for arquivo in sorted((ROOT_DIR / diretorio).rglob("*.py")):
            if "__pycache__" in arquivo.parts:
                continue
            texto = arquivo.read_text(encoding="utf-8")
            arvore = ast.parse(texto, filename=str(arquivo))
            visitante = _Visitante()
            visitante.visit(arvore)
            linhas = texto.splitlines()
            for no in ast.walk(arvore):
                if not isinstance(no, ast.Attribute) or no.attr not in colunas:
                    continue
                if not isinstance(no.ctx, ast.Load):
                    continue
                # `Modelo.coluna` em expressao de SQL nao le valor nenhum: e a
                # coluna, e o `None` vira `IS NULL` do lado do banco.
                if isinstance(no.value, ast.Name) and no.value.id[:1].isupper():
                    continue
                if _protegido(no, visitante.pais):
                    continue
                forma = _forma_que_quebra(no, visitante.pais)
                if forma is None:
                    continue
                achados.append(
                    (
                        str(arquivo.relative_to(ROOT_DIR)).replace("\\", "/"),
                        no.lineno,
                        no.attr,
                        forma,
                        linhas[no.lineno - 1].strip()[:100],
                    )
                )
    return achados


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acha leitura de coluna nulavel em forma que quebra com None."
    )
    parser.add_argument("--url", help="URL do banco. Sem ela, usa a DATABASE_URL do ambiente.")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--coluna", help="Investigar uma coluna so.")
    parser.add_argument(
        "--limite",
        type=int,
        help=(
            "Quantas leituras sao esperadas hoje. Passar deste numero vira AVISO "
            "(anotacao do GitHub Actions), nunca falha."
        ),
    )
    args = parser.parse_args()

    engine = create_engine(args.url) if args.url else get_engine()
    inspetor = inspect(engine)
    if not inspetor.get_table_names(schema=args.schema):
        print(f"Nenhuma tabela no schema '{args.schema}'. A URL aponta para o banco certo?")
        return 1

    divergentes = {coluna for _, coluna in comparar(inspetor, args.schema).orm_mais_estrito}
    if args.coluna:
        divergentes = {args.coluna} & divergentes or {args.coluna}

    achados = leituras(divergentes)

    print("=" * 76)
    print(
        f"{len(divergentes)} coluna(s) que o banco deixa nula  |  "
        f"{len(achados)} leitura(s) em forma que quebra"
    )
    print("=" * 76)

    por_coluna: dict[str, list] = {}
    for achado in achados:
        por_coluna.setdefault(achado[2], []).append(achado)

    for coluna in sorted(por_coluna):
        print()
        print(f"## {coluna}  ({len(por_coluna[coluna])})")
        print()
        for arquivo, linha, _, forma, fonte in por_coluna[coluna]:
            print(f"  {arquivo}:{linha}  [{forma}]")
            print(f"      {fonte}")

    if not achados:
        print()
        print("Nenhuma. Toda leitura dessas colunas trata o nulo.")

    print()
    print("Nem toda leitura daqui e defeito: o casamento e por NOME de atributo,")
    print("e o script nao sabe de qual tabela o objeto veio. O que ele garante e")
    print("que nenhuma leitura DESSA FORMA passou despercebida.")

    if args.limite is not None:
        if len(achados) > args.limite:
            print()
            print(
                f"::warning title=Leitura de coluna nulavel::{len(achados)} leitura(s) "
                f"em forma que quebra com None, e o esperado era {args.limite}. "
                f"{len(achados) - args.limite} nova(s). Ver a saida acima e "
                "docs/alinhamento-orm-schema.md."
            )
        elif len(achados) < args.limite:
            print()
            print(
                f"::notice title=Leitura de coluna nulavel::{len(achados)} leitura(s), "
                f"abaixo do limite de {args.limite}. Baixe o --limite no ci.yml."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
