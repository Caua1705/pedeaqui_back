"""A constante do codigo e o CHECK do banco falam a MESMA lista — a armadilha 15.

A armadilha 15 diz, com o caso concreto:

    `PAYMENT_METHODS` espelha o CHECK de `branch_payment_methods.method_type`.
    Se um metodo entrar no banco e nao na constante, a filial consegue
    oferece-lo no cardapio e o pedido e **recusado na criacao** com 400. O
    lojista ve a opcao na tela do cliente e o cliente nao consegue fechar.

Sao duas escritas da mesma lista, em lugares que nao se olham: uma revisao do
Alembic e um `.py`. Nada as obriga a mudar juntas, e o sintoma nao aparece em
nenhum dos dois lados — aparece no checkout de um cliente, uma vez, sem log.

## O que este script faz

Le TODO `CHECK (col = ANY (ARRAY[...]))` do banco e cobra que cada um esteja
declarado em `ESPELHOS` — apontando para o conjunto do codigo que o espelha,
ou dizendo por que nao ha espelho. Depois compara valor a valor.

O espelho e resolvido em quatro formas, porque e assim que os conjuntos
fechados existem neste repositorio:

    src/core/constants.py:PAYMENT_METHODS       tupla de literais
    src/models/coupon_model.py:COUPON_VISIBILITIES  tupla de constantes
    src/schemas/admin_customer_schema.py:CustomerSegment   classe `str, Enum`
    src/schemas/coupon_schema.py:DiscountType     alias de `Literal[...]`
    src/schemas/ai_feedback_schema.py:AIFeedbackRequest.feedback   campo `Literal[...]`

## Por que registro explicito, e nao "procure a constante com os mesmos valores"

Casar por igualdade de valores parece mais esperto e e inutil: quando as duas
listas DIVERGEM — que e o unico caso que importa — a busca por igualdade nao
acha nada, e o resultado fica indistinguivel de "esta coluna nao tem
espelho". O registro diz qual e o par; a comparacao diz se ele bate.

`SEM_ESPELHO` e uma resposta valida e escrita, para as colunas cujos valores
so existem soltos no codigo. Ela nao e "tudo bem": e a fronteira do que este
portao alcanca, anotada onde da para ver.

## E de brinde: constraint DUPLICADA

Duas CHECK com nomes diferentes sobre a MESMA coluna e a MESMA lista sao
avaliadas as duas em toda escrita — custo por nada. E a armadilha 4 (indice
duplicado) na forma de constraint, e `scripts/audit_indexes.py` nao a
encontra porque ele olha `pg_index`, nao `pg_constraint`.

`admin_users` tinha uma, achada aqui na primeira execucao e derrubada pela
revisao `20260904_0048`. A checagem ficou porque a proxima entra do mesmo
jeito: um CHECK inline no `CREATE TABLE` ganha o nome que o Postgres gera, e
alguem escreve o `ck_` a mao depois.

## Onde ele nao chega

Ele le o banco de TESTE (o mesmo `alembic upgrade head` do CI), nao o de
producao. Coluna acrescentada a mao no Supabase nao aparece aqui — e a
armadilha 33, e o que a fecha e a regra de so mudar schema por revisao.

    python scripts/espelhos_de_enum.py --url postgresql+psycopg://...

Somente leitura: le codigo-fonte e `pg_constraint`. Nao escreve nada.
"""

import argparse
import ast
import re
from pathlib import Path

from sqlalchemy import create_engine, text


ROOT_DIR = Path(__file__).resolve().parents[1]

# `(col = ANY (ARRAY['a'::text, 'b'::text]))`, que e como o Postgres devolve
# um `IN` depois de guardado.
RE_COLUNA = re.compile(r"\(?([A-Za-z_]\w*)\s*=\s*ANY\s*\(\s*ARRAY\[", re.IGNORECASE)
RE_VALOR = re.compile(r"'([^']*)'::text")

MINIMO_DE_VALORES = 2

SEM_ESPELHO = None


# constraint -> caminho do conjunto no codigo, ou (SEM_ESPELHO, motivo).
#
# Toda coluna de enum do banco entra aqui. Uma que apareca e nao esteja na
# lista e achado: e a revisao nova que ninguem conferiu contra o codigo.
ESPELHOS: dict[str, str | tuple[None, str]] = {
    # --- os pares de verdade ------------------------------------------------
    "branch_payment_methods_method_type_check": "src/core/constants.py:PAYMENT_METHODS",
    "orders_status_check": "src/core/constants.py:ORDER_STATUSES",
    "ck_orders_payment_status": "src/core/constants.py:PAYMENT_STATUSES",
    "orders_order_type_check": "src/core/constants.py:ORDER_TYPES",
    "ck_order_reviews_problem_tag": "src/core/constants.py:REVIEW_PROBLEM_TAGS",
    "ck_admin_users_role": "src/core/constants.py:ADMIN_USER_ROLES",
    "ck_restaurant_coupons_visibility": "src/models/coupon_model.py:COUPON_VISIBILITIES",
    "ck_restaurant_coupons_target_segment": "src/schemas/admin_customer_schema.py:CustomerSegment",
    "ck_ai_usage_events_surface": "src/models/ai_usage_event_model.py:AI_SURFACES",
    "coupon_templates_discount_type_check": "src/schemas/coupon_schema.py:DiscountType",
    "restaurant_coupons_discount_type_valid": "src/schemas/coupon_schema.py:DiscountType",
    "ai_feedback_feedback_check": "src/schemas/ai_feedback_schema.py:AIFeedbackRequest.feedback",
    # --- as colunas cujos valores so existem soltos -------------------------
    "ck_orders_payment_flow": (
        SEM_ESPELHO,
        "`online`/`delivery` vivem soltos como literal em `_resolve_payment_flow` "
        "e nos services de pagamento. Declarar a constante os tornaria "
        "visiveis tambem para `filtros_por_exclusao.py`, que hoje nao os ve",
    ),
    "branch_payment_methods_payment_flow_check": (
        SEM_ESPELHO,
        "O par de `ck_orders_payment_flow`, na tabela que oferece o metodo. "
        "Mesma lista, mesmo motivo, e por isso as duas divergiriam juntas",
    ),
    "ck_cashback_transactions_type": (
        SEM_ESPELHO,
        "Os cinco tipos do razao de cashback so existem como literal em "
        "`cashback_service.py` e em `scripts/expire_cashback.py`. O segundo "
        "grava `status='expired'` a mao, e e ele que a armadilha 26 cita",
    ),
    "ck_cashback_transactions_status": (
        SEM_ESPELHO,
        "Par do de cima, e o mesmo caso",
    ),
    "coupon_redemptions_status_valid": (
        SEM_ESPELHO,
        "`applied`/`reversed` sao literais em `coupon_repository`. E o "
        "conjunto que `filtros_por_exclusao.py` cita como exemplo do que ele "
        "NAO enxerga: nao ha como saber que sao dois se ninguem escreveu os dois",
    ),
    "ck_idempotency_keys_status": (
        SEM_ESPELHO,
        "Ha as constantes `IDEMPOTENCY_IN_PROGRESS` e `IDEMPOTENCY_COMPLETED` "
        "em `idempotency_key_model.py`, mas soltas — nao ha tupla com as duas",
    ),
    "ck_restaurant_payment_credentials_environment": (
        SEM_ESPELHO,
        "`test`/`production` vem de `MERCADOPAGO_ENVIRONMENT`, validada no "
        "`startup_checks` contra a tupla literal daquela linha",
    ),
    "ck_customer_payment_profiles_environment": (
        SEM_ESPELHO,
        "Par do de cima, e o mesmo caso",
    ),
    "restaurant_banners_banner_type_check": (
        SEM_ESPELHO,
        "`hero`/`highlight` sao literais nas duas chamadas de "
        "`get_banners_by_type`, em `menu_service.py`",
    ),
    "restaurant_coupons_discount_value_valid": (
        SEM_ESPELHO,
        "Nao e lista de dominio: e a regra que amarra VALOR ao tipo "
        "(`free_delivery` com zero, os outros dois com positivo). Os tipos "
        "aparecem nela de passagem, e quem os declara e "
        "`restaurant_coupons_discount_type_valid`",
    ),
}


class Achado:
    def __init__(self, tipo: str, constraint: str, detalhe: str):
        self.tipo = tipo
        self.constraint = constraint
        self.detalhe = detalhe

    def __str__(self) -> str:
        return f"{self.constraint}\n      {self.detalhe}"


def _colunas_de_enum(url: str) -> dict[str, dict]:
    """Toda coluna com `CHECK (col = ANY (ARRAY[...]))`, por nome de constraint."""
    engine = create_engine(url)
    with engine.connect() as conexao:
        linhas = conexao.execute(
            text(
                "SELECT conrelid::regclass::text AS tabela, conname, "
                "pg_get_constraintdef(oid) AS definicao "
                "FROM pg_constraint WHERE contype = 'c' ORDER BY 1, 2"
            )
        ).all()

    encontradas = {}
    for tabela, constraint, definicao in linhas:
        valores = set(RE_VALOR.findall(definicao))
        coluna = RE_COLUNA.search(definicao)
        if len(valores) < MINIMO_DE_VALORES or coluna is None:
            continue
        encontradas[constraint] = {
            "tabela": tabela,
            "coluna": coluna.group(1),
            "valores": valores,
            "definicao": " ".join(definicao.split()),
        }
    return encontradas


def _literais(no: ast.AST) -> set[str] | None:
    """Os valores de `("a", "b")`, `frozenset({...})` ou `Literal["a", "b"]`."""
    if isinstance(no, ast.Subscript) and "Literal" in ast.unparse(no.value):
        indice = no.slice
        elementos = indice.elts if isinstance(indice, ast.Tuple) else [indice]
    elif isinstance(no, (ast.Tuple, ast.List, ast.Set)):
        elementos = no.elts
    elif isinstance(no, ast.Call) and isinstance(no.func, ast.Name) and no.func.id == "frozenset":
        if not no.args or not isinstance(no.args[0], (ast.Tuple, ast.List, ast.Set)):
            return None
        elementos = no.args[0].elts
    else:
        return None

    valores = set()
    for elemento in elementos:
        if not isinstance(elemento, ast.Constant) or not isinstance(elemento.value, str):
            return None
        valores.add(elemento.value)
    return valores or None


class ConjuntosDoCodigo:
    """Os conjuntos fechados do `src/`, enderecaveis por `arquivo.py:NOME`.

    `NOME` pode ser uma constante de modulo, uma classe `str, Enum`, um alias
    de `Literal[...]` ou `Classe.campo` — as quatro formas em que uma lista
    fechada e escrita neste repositorio.
    """

    def __init__(self, raiz: Path):
        self.por_caminho: dict[str, set[str]] = {}
        self.literais: dict[str, str] = {}
        for arquivo in sorted((raiz / "src").rglob("*.py")):
            if "__pycache__" in arquivo.parts:
                continue
            relativo = arquivo.relative_to(raiz).as_posix()
            arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
            self._ler_modulo(relativo, arvore)
            self._ler_classes(relativo, arvore)

    def _ler_modulo(self, relativo: str, arvore: ast.Module) -> None:
        for no in arvore.body:
            alvo, valor = self._alvo_e_valor(no)
            if alvo is None:
                continue
            if isinstance(valor, ast.Constant) and isinstance(valor.value, str):
                self.literais[alvo] = valor.value
                continue
            valores = _literais(valor) or self._tupla_de_constantes(valor)
            if valores:
                self.por_caminho[f"{relativo}:{alvo}"] = valores

    def _tupla_de_constantes(self, no: ast.AST) -> set[str] | None:
        """`(COUPON_VISIBILITY_PUBLIC, ...)` — tupla de nomes ja vistos."""
        if not isinstance(no, (ast.Tuple, ast.List, ast.Set)):
            return None
        valores = set()
        for elemento in no.elts:
            if not isinstance(elemento, ast.Name) or elemento.id not in self.literais:
                return None
            valores.add(self.literais[elemento.id])
        return valores or None

    def _ler_classes(self, relativo: str, arvore: ast.Module) -> None:
        for no in ast.walk(arvore):
            if not isinstance(no, ast.ClassDef):
                continue
            if any("Enum" in ast.unparse(base) for base in no.bases):
                valores = {
                    corpo.value.value
                    for corpo in no.body
                    if isinstance(corpo, ast.Assign)
                    and isinstance(corpo.value, ast.Constant)
                    and isinstance(corpo.value.value, str)
                }
                if valores:
                    self.por_caminho[f"{relativo}:{no.name}"] = valores
            for corpo in no.body:
                if not isinstance(corpo, ast.AnnAssign) or not isinstance(corpo.target, ast.Name):
                    continue
                valores = _literais(corpo.annotation)
                if valores:
                    self.por_caminho[f"{relativo}:{no.name}.{corpo.target.id}"] = valores

    @staticmethod
    def _alvo_e_valor(no: ast.AST) -> tuple[str | None, ast.AST | None]:
        if isinstance(no, ast.Assign) and len(no.targets) == 1 and isinstance(no.targets[0], ast.Name):
            return no.targets[0].id, no.value
        if isinstance(no, ast.AnnAssign) and isinstance(no.target, ast.Name):
            return no.target.id, no.value
        return None, None


def auditar(url: str, raiz: Path | None = None) -> list[Achado]:
    raiz = raiz or ROOT_DIR
    colunas = _colunas_de_enum(url)
    codigo = ConjuntosDoCodigo(raiz)
    achados: list[Achado] = []

    for constraint, dados in sorted(colunas.items()):
        if constraint not in ESPELHOS:
            achados.append(
                Achado(
                    "sem registro",
                    constraint,
                    f"{dados['tabela']}.{dados['coluna']} tem "
                    f"{len(dados['valores'])} valores e nao esta em ESPELHOS. "
                    "Aponte para a constante que a espelha, ou declare "
                    "SEM_ESPELHO com o motivo.",
                )
            )
            continue

        espelho = ESPELHOS[constraint]
        if not isinstance(espelho, str):
            continue

        do_codigo = codigo.por_caminho.get(espelho)
        if do_codigo is None:
            achados.append(
                Achado("espelho sumiu", constraint, f"`{espelho}` nao existe mais no codigo.")
            )
            continue
        if do_codigo != dados["valores"]:
            so_no_banco = sorted(dados["valores"] - do_codigo)
            so_no_codigo = sorted(do_codigo - dados["valores"])
            achados.append(
                Achado(
                    "divergem",
                    constraint,
                    f"{dados['tabela']}.{dados['coluna']} x `{espelho}`\n"
                    f"      so no BANCO:  {so_no_banco}   (o codigo recusa o que a tela oferece)\n"
                    f"      so no CODIGO: {so_no_codigo}   (o banco recusa o que o codigo aceita)",
                )
            )

    for constraint in sorted(ESPELHOS):
        if constraint not in colunas:
            achados.append(
                Achado(
                    "constraint sumiu",
                    constraint,
                    "Declarada em ESPELHOS e nao existe no banco: lista velha.",
                )
            )

    achados.extend(_duplicatas(colunas))
    return achados


def _duplicatas(colunas: dict[str, dict]) -> list[Achado]:
    """Duas CHECK com a MESMA DEFINICAO na mesma tabela.

    O Postgres avalia as duas em toda escrita, e nenhuma recusa nada que a
    outra ja nao recusasse. E a armadilha 4 na forma de constraint — o
    `audit_indexes.py` olha `pg_index` e nao ve isto.

    **O criterio e a definicao inteira, e nao (tabela, coluna, valores).** A
    primeira versao usava a segunda forma e acusou um par que nao e duplicata:
    `restaurant_coupons_discount_type_valid` (a lista de tipos) e
    `restaurant_coupons_discount_value_valid` (a regra que amarra VALOR ao
    tipo) falam da mesma coluna e citam os mesmos tres literais, e sao regras
    diferentes — derrubar uma delas abriria cupom percentual de valor zero.

    Falso positivo aqui e caro de um jeito especifico: o achado pede um DROP,
    e um DROP sobre a constraint errada nao da erro nenhum no dia em que roda.
    """
    por_definicao: dict[tuple[str, str], list[str]] = {}
    for constraint, dados in sorted(colunas.items()):
        por_definicao.setdefault((dados["tabela"], dados["definicao"]), []).append(constraint)

    achados = []
    for (tabela, _), nomes in sorted(por_definicao.items()):
        if len(nomes) < 2:
            continue
        achados.append(
            Achado(
                "duplicata",
                " + ".join(nomes),
                f"{tabela} tem {len(nomes)} CHECK com a definicao IDENTICA, e o "
                "Postgres avalia todas em toda escrita. Derrube as sobras numa "
                "revisao, mantendo o nome no padrao `ck_<tabela>_<coluna>`.",
            )
        )
    return achados


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Confere as constantes do codigo contra os CHECK de enum do banco."
    )
    parser.add_argument("--url", required=True, help="URL do Postgres (SQLAlchemy)")
    parser.add_argument("--tudo", action="store_true", help="lista tambem os pares que batem")
    args = parser.parse_args()

    achados = auditar(args.url)
    colunas = _colunas_de_enum(args.url)

    print("=" * 78)
    print(f"{len(achados)} achado(s)  |  {len(colunas)} coluna(s) de enum no banco")
    print("=" * 78)
    print()
    if not achados:
        print("  Nenhum: toda coluna de enum esta declarada, e todo espelho bate.")
    for achado in achados:
        print(f"  [{achado.tipo}] {achado}")
        print()

    if args.tudo:
        print("## O que cada coluna de enum espelha")
        print()
        for constraint in sorted(colunas):
            espelho = ESPELHOS.get(constraint, "(fora de ESPELHOS)")
            if isinstance(espelho, tuple):
                print(f"  {constraint}\n      SEM ESPELHO: {espelho[1]}")
            else:
                print(f"  {constraint}\n      {espelho}")

    print()
    print("Divergencia aqui nao da erro em lugar nenhum: o valor que existe so")
    print("no banco e oferecido na tela e recusado no checkout, e o que existe")
    print("so no codigo morre no INSERT. Os dois aparecem no cliente, uma vez.")
    return 1 if achados else 0


if __name__ == "__main__":
    raise SystemExit(main())
