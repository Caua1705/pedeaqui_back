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

E, numa SEGUNDA secao, a outra metade: **todo campo de schema Pydantic que
declara uma dessas colunas sem `| None`**. Ela nao aparece na varredura de
leituras — um campo e anotacao de classe, nao acesso a atributo — e e por ali
que a maioria dos 500 nasce, derrubando a resposta INTEIRA e nao so o item
quebrado.

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


def campos_de_schema_sem_nulo(colunas: set[str]) -> list[tuple[str, str, str]]:
    """Schema de resposta que declara a coluna SEM `| None`.

    E a OUTRA metade, e ela nao aparece na varredura de leituras: um campo
    Pydantic e uma anotacao de classe, nao um acesso a atributo. E e por aqui
    que a maioria dos 500 nasce — `None` chegando num campo obrigatorio vira
    `ValidationError` na serializacao, e derruba a resposta INTEIRA (a lista,
    nao so o item quebrado).

    Mesma ressalva das leituras, e mais forte: casa por NOME. `email` e campo
    de meia duzia de schemas, e a maioria nao tem nada a ver com
    `customers.email`. Serve para nao deixar nenhum passar, nao para acusar.
    """
    import importlib
    import pkgutil

    from pydantic import BaseModel

    import src

    for info in pkgutil.walk_packages(src.__path__, prefix="src."):
        try:
            importlib.import_module(info.name)
        except Exception:
            continue

    achados = []
    vistos: set[type] = set()
    for nome_do_modulo, modulo in list(sys.modules.items()):
        if not nome_do_modulo.startswith("src.") or modulo is None:
            continue
        for nome in dir(modulo):
            objeto = getattr(modulo, nome, None)
            if not isinstance(objeto, type) or objeto in vistos:
                continue
            if not (issubclass(objeto, BaseModel) and objeto is not BaseModel):
                continue
            vistos.add(objeto)
            for campo, info_do_campo in objeto.model_fields.items():
                if campo not in colunas:
                    continue
                anotacao = str(info_do_campo.annotation)
                if "NoneType" in anotacao or "Optional" in anotacao:
                    continue
                achados.append((objeto.__name__, campo, anotacao.replace("<class '", "").replace("'>", "")))
    return sorted(set(achados))


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

    campos = campos_de_schema_sem_nulo(divergentes)
    print()
    print("=" * 76)
    print(f"E {len(campos)} campo(s) de schema declarando essas colunas SEM `| None`")
    print("=" * 76)
    print()
    print("  Aqui nasce a maioria dos 500: `None` num campo obrigatorio e")
    print("  ValidationError na serializacao, e derruba a resposta INTEIRA.")
    print()
    for schema, campo, tipo in campos:
        print(f"  {schema}.{campo}: {tipo}")

    print()
    print("Nem tudo daqui e defeito: o casamento e por NOME, e o script nao sabe")
    print("de qual tabela o objeto veio. O que ele garante e que nenhuma leitura")
    print("nem nenhum campo DESSA FORMA passou despercebido.")

    total = len(achados) + len(campos)
    if args.limite is not None:
        if total > args.limite:
            print()
            print(
                f"::warning title=Coluna nulavel lida como se nao fosse::{total} "
                f"ponto(s) — {len(achados)} leitura(s) e {len(campos)} campo(s) de "
                f"schema —, e o esperado era {args.limite}. {total - args.limite} "
                "novo(s). Ver a saida acima e docs/alinhamento-orm-schema.md."
            )
        elif total < args.limite:
            print()
            print(
                f"::notice title=Coluna nulavel lida como se nao fosse::{total} ponto(s), "
                f"abaixo do limite de {args.limite}. Baixe o --limite no ci.yml."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
