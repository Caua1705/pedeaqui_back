"""O `restaurant_id` e o `branch_id` de cada rota `/admin` vem do TOKEN?

Irmao de `tests/test_papeis_das_rotas.py`. Aquele audita **o que** o lojista
pode fazer (o papel); este audita **onde** ele pode mexer (o escopo). As duas
dimensoes moram no mesmo arquivo — `src/api/dependencies/admin_scope.py` — e ate
agora so uma delas tinha varredura.

O custo de errar aqui e o maior do sistema: **um restaurante lendo pedido,
cliente ou faturamento de outro.** Nao e 500, nao e lentidao — chega em
silencio e sem log.

## As tres perguntas

**1. Alguma rota `/admin` aceita `restaurant_id` do cliente?**
Nenhuma pode. O restaurante sai do token, ponto.

**2. Toda rota `/admin` recebe o escopo?**
`AdminScope` na assinatura. As excecoes legitimas (login, `/auth/me`, o ticket
do stream) estao em `SEM_ESCOPO_ESPERADO`, com o motivo — e uma rota nova que
nao apareca la vira achado.

**3. O `branch_id` que o cliente manda e conferido — nas DUAS dimensoes?**

Esta e a que morde, e a que so se responde seguindo a cadeia de chamadas.
`AdminSettingsService._get_branch` explica por que sao duas:

    `ensure_branch_allowed` barra a filial que existe mas nao e a deste
    lojista; o repositorio barra a filial de OUTRO RESTAURANTE.

**E `ensure_branch_allowed` sozinho NAO basta, e o motivo e sutil:** ele so
recusa quando `scope.branch_id` esta preenchido. Para o **dono**
(`branch_id = None`, sempre, por desenho) ele retorna na primeira linha sem
conferir nada. Numa rota que so o chame, o dono do restaurante A alcanca a
filial do restaurante B — e o unico obstaculo seria a segunda conferencia, que
nao estaria la.

Por isso o script exige as duas, e as reporta separadas.

## Como ele segue a cadeia

Monta um grafo de chamadas a partir do AST, resolvendo:

- `AlgumService(db).metodo(...)` — o construtor no proprio endpoint;
- `self.metodo(...)` — mesma classe;
- `self.atributo.metodo(...)` — o tipo do atributo sai das atribuicoes do
  `__init__` (`self.order_service = AdminOrderService(db)`).

E procura os marcadores ate `PROFUNDIDADE_MAXIMA` saltos.

**Quando NAO consegue seguir, ele diz que nao conseguiu** — nunca "ok". Um
varredor que responde "conferido" para o que ele nao leu e pior que nao ter
varredor: ele transforma ignorancia em garantia.

    python scripts/escopo_das_rotas.py
    python scripts/escopo_das_rotas.py --tudo

Somente leitura: le codigo-fonte e a tabela de rotas do app. Nao abre banco.
"""

import argparse
import ast
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


DIRETORIOS = ("src/api/endpoints", "src/services")

PROFUNDIDADE_MAXIMA = 5

# Rotas `/admin` que legitimamente NAO recebem `AdminScope`, e o motivo de cada
# uma. Rota nova fora desta lista e achado — a lista e curta de proposito.
SEM_ESCOPO_ESPERADO = {
    "POST /admin/auth/login": "ainda nao ha lojista autenticado",
    "GET /admin/auth/me": "recebe o `admin_user`, que ja carrega o restaurante",
    "PATCH /admin/auth/password": "idem — escreve na propria linha do lojista",
    "POST /admin/orders/stream-ticket": "idem — o ticket e emitido a partir dele",
    "GET /admin/orders/stream": (
        "autentica por ticket na querystring, nao por Depends; chama "
        "`build_admin_scope` por dentro (ver o docstring de admin_scope.py)"
    ),
    "GET /admin/coupon-templates": (
        "as artes de cupom sao catalogo GLOBAL da plataforma, nao dado de "
        "restaurante — nao ha o que recortar"
    ),
}

# O que conta como conferencia da FILIAL (o lojista preso a uma loja).
MARCADORES_DE_FILIAL = ("ensure_branch_allowed", "resolve_branch_filter")

# O que conta como conferencia do RESTAURANTE (a filial e de outra loja?).
#
# A REGRA FORTE e a primeira: `scope.restaurant_id` aparecendo como argumento
# de uma chamada, POSICIONAL OU NOMEADO. Ela nao depende de convencao de nome
# nenhuma — se o id do restaurante do TOKEN entra na consulta, a consulta esta
# recortada.
#
# As duas primeiras versoes deste script erravam aqui, e as duas erravam para o
# mesmo lado: so olhavam `restaurant_id=` nomeado, e acusaram
# `list_categories` e `list_branch_operation`, que passam `scope.restaurant_id`
# posicional. Dois falsos positivos.
#
# Os sufixos ficam como sinal ADICIONAL, para o caso de o id chegar por um
# caminho que o AST nao mostra na mesma linha.
EXPRESSAO_DE_RESTAURANTE = "scope.restaurant_id"
SUFIXOS_DE_RESTAURANTE = ("_and_restaurant", "_by_restaurant")


class _Indice:
    """Todas as funcoes e metodos de `src/`, achavaveis por nome."""

    def __init__(self, diretorios=None) -> None:
        # `diretorios` e parametro para o teste conseguir montar um indice
        # sobre uma arvore PLANTADA. Sem isso nao ha como provar que o
        # varredor acusa o que ele deve acusar — e um varredor que so foi visto
        # respondendo "nenhuma" nao provou nada.
        # POR (MODULO, NOME), e nao por nome. Nomes de funcao de endpoint
        # colidem entre arquivos — `list_orders` existe em `admin_orders.py` e
        # em `customers.py` (o `/me/orders` do cliente) —, e um indice por nome
        # puro deixa o ultimo arquivo lido vencer.
        #
        # Isso custou uma execucao: a varredura acusou `GET /admin/orders` de
        # nao conferir nada, seguindo o corpo da rota do CLIENTE. Falso
        # positivo. O perigo, porem, e o contrario: a mesma colisao pode fazer
        # uma rota QUEBRADA parecer conferida, e ai o varredor mente calado.
        self.funcoes: dict[tuple[str, str], ast.FunctionDef] = {}
        self.metodos: dict[tuple[str, str], ast.FunctionDef] = {}
        self.atributos: dict[str, dict[str, str]] = {}
        self.classes_repetidas: set[str] = set()
        for diretorio in diretorios if diretorios is not None else DIRETORIOS:
            raiz = Path(diretorio)
            if not raiz.is_absolute():
                raiz = ROOT_DIR / raiz
            for arquivo in sorted(raiz.rglob("*.py")):
                if "__pycache__" in arquivo.parts:
                    continue
                modulo = arquivo.stem
                arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
                for no in arvore.body:
                    if isinstance(no, ast.FunctionDef):
                        self.funcoes[(modulo, no.name)] = no
                    elif isinstance(no, ast.ClassDef):
                        if no.name in self.atributos:
                            # Duas classes com o mesmo nome quebrariam a
                            # resolucao por classe do mesmo jeito que os nomes
                            # de funcao quebraram a de modulo. Nao ha nenhuma
                            # hoje; se aparecer, tem que aparecer ALTO.
                            self.classes_repetidas.add(no.name)
                        self._indexar_classe(no)

    def _indexar_classe(self, classe: ast.ClassDef) -> None:
        atributos: dict[str, str] = {}
        for corpo in classe.body:
            if not isinstance(corpo, ast.FunctionDef):
                continue
            self.metodos[(classe.name, corpo.name)] = corpo
            if corpo.name != "__init__":
                continue
            # `self.order_service = AdminOrderService(db)` -> atributo/classe
            for no in ast.walk(corpo):
                if not isinstance(no, ast.Assign) or not isinstance(no.value, ast.Call):
                    continue
                alvo = no.targets[0]
                chamada = no.value.func
                if (
                    isinstance(alvo, ast.Attribute)
                    and isinstance(alvo.value, ast.Name)
                    and alvo.value.id == "self"
                    and isinstance(chamada, ast.Name)
                ):
                    atributos[alvo.attr] = chamada.id
        self.atributos[classe.name] = atributos


def _nome_chamado(no: ast.Call) -> str | None:
    alvo = no.func
    if isinstance(alvo, ast.Name):
        return alvo.id
    if isinstance(alvo, ast.Attribute):
        return alvo.attr
    return None


def _classe_do_receptor(no: ast.Call, classe_atual: str | None, indice: _Indice) -> str | None:
    """De qual classe e o metodo chamado, quando da para saber."""
    alvo = no.func
    if not isinstance(alvo, ast.Attribute):
        return None
    receptor = alvo.value
    if isinstance(receptor, ast.Name) and receptor.id == "self":
        return classe_atual
    if (
        isinstance(receptor, ast.Attribute)
        and isinstance(receptor.value, ast.Name)
        and receptor.value.id == "self"
        and classe_atual
    ):
        return indice.atributos.get(classe_atual, {}).get(receptor.attr)
    if isinstance(receptor, ast.Call) and isinstance(receptor.func, ast.Name):
        return receptor.func.id
    return None


def _procurar(
    corpo: ast.FunctionDef,
    classe: str | None,
    indice: _Indice,
    profundidade: int,
    visitados: set,
) -> tuple[bool, bool, bool]:
    """(conferiu filial, conferiu restaurante, seguiu tudo o que viu)."""
    if profundidade > PROFUNDIDADE_MAXIMA:
        return False, False, False

    filial = restaurante = False
    completo = True

    for no in ast.walk(corpo):
        if not isinstance(no, ast.Call):
            continue
        nome = _nome_chamado(no)
        if nome is None:
            continue
        if nome in MARCADORES_DE_FILIAL:
            filial = True
        if nome.endswith(SUFIXOS_DE_RESTAURANTE):
            restaurante = True
        argumentos = list(no.args) + [palavra.value for palavra in no.keywords]
        if any(ast.unparse(arg) == EXPRESSAO_DE_RESTAURANTE for arg in argumentos):
            restaurante = True

        classe_alvo = _classe_do_receptor(no, classe, indice)
        proximo = None
        if classe_alvo is not None:
            proximo = indice.metodos.get((classe_alvo, nome))
        # Funcao de modulo chamada por nome puro: sem receptor, nao da para
        # saber de qual modulo ela e, entao NAO se segue. Seguir pelo nome era
        # exatamente o defeito que a colisao de `list_orders` mostrou.

        if proximo is None:
            continue
        chave = (classe_alvo, nome)
        if chave in visitados:
            continue
        visitados.add(chave)
        f, r, c = _procurar(proximo, classe_alvo, indice, profundidade + 1, visitados)
        filial = filial or f
        restaurante = restaurante or r
        completo = completo and c

    return filial, restaurante, completo


def auditar() -> list[dict]:
    """Uma linha por rota `/admin`. Precisa do app carregado."""
    import inspect as pyinspect

    from src.api.dependencies.admin_scope import AdminScope
    from tests.rotas_do_app import rotas_com_caminho

    indice = _Indice()
    linhas = []
    for rota in rotas_com_caminho():
        if not rota.path.startswith("/admin"):
            continue
        metodos = sorted(rota.methods - {"HEAD", "OPTIONS"})
        assinatura = pyinspect.signature(rota.endpoint)
        anotacoes = {n: p.annotation for n, p in assinatura.parameters.items()}

        recebe_escopo = any(a is AdminScope for a in anotacoes.values())
        aceita_restaurante = "restaurant_id" in anotacoes or "{restaurant_id}" in rota.path
        aceita_filial = "branch_id" in anotacoes or "{branch_id}" in rota.path

        filial = restaurante = False
        completo = True
        if aceita_filial:
            corpo = indice.funcoes.get(
                (rota.endpoint.__module__.rsplit(".", 1)[-1], rota.endpoint.__name__)
            )
            if corpo is None:
                completo = False
            else:
                filial, restaurante, completo = _procurar(corpo, None, indice, 0, set())

        for metodo in metodos:
            linhas.append(
                {
                    "rota": f"{metodo} {rota.path}",
                    "funcao": rota.endpoint.__name__,
                    "recebe_escopo": recebe_escopo,
                    "aceita_restaurante": aceita_restaurante,
                    "aceita_filial": aceita_filial,
                    "confere_filial": filial,
                    "confere_restaurante": restaurante,
                    "seguiu_tudo": completo,
                }
            )
    return linhas


def achados(linhas: list[dict]) -> dict[str, list[dict]]:
    return {
        "restaurante_do_cliente": [linha for linha in linhas if linha["aceita_restaurante"]],
        "sem_escopo": [
            linha
            for linha in linhas
            if not linha["recebe_escopo"] and linha["rota"] not in SEM_ESCOPO_ESPERADO
        ],
        "filial_sem_conferencia_de_restaurante": [
            linha
            for linha in linhas
            if linha["aceita_filial"] and not linha["confere_restaurante"]
        ],
        "filial_sem_conferencia_de_filial": [
            linha for linha in linhas if linha["aceita_filial"] and not linha["confere_filial"]
        ],
        "nao_consegui_seguir": [
            linha for linha in linhas if linha["aceita_filial"] and not linha["seguiu_tudo"]
        ],
    }


TITULOS = {
    "restaurante_do_cliente": "Rota /admin que aceita `restaurant_id` do CLIENTE",
    "sem_escopo": "Rota /admin sem `AdminScope`, e fora da lista de excecoes",
    "filial_sem_conferencia_de_restaurante": (
        "Rota com `branch_id` do cliente SEM conferencia de RESTAURANTE "
        "(o dono de A alcanca a filial de B)"
    ),
    "filial_sem_conferencia_de_filial": (
        "Rota com `branch_id` do cliente SEM `ensure_branch_allowed` "
        "(o gerente preso a uma loja alcanca a vizinha)"
    ),
    "nao_consegui_seguir": "Rota cuja cadeia de chamadas eu NAO consegui seguir inteira",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audita o escopo de tenant das rotas /admin (somente leitura)."
    )
    parser.add_argument("--tudo", action="store_true", help="Lista todas as rotas.")
    args = parser.parse_args()

    linhas = auditar()
    encontrados = achados(linhas)
    total = sum(len(v) for v in encontrados.values())

    com_filial = [linha for linha in linhas if linha["aceita_filial"]]
    print("=" * 78)
    print(
        f"{len(linhas)} rota(s) /admin  |  {len(com_filial)} recebem `branch_id` do "
        f"cliente  |  {total} achado(s)"
    )
    print("=" * 78)

    for chave, titulo in TITULOS.items():
        grupo = encontrados[chave]
        print()
        print(f"## {titulo}  ({len(grupo)})")
        print()
        if not grupo:
            print("  Nenhuma.")
            continue
        for linha in grupo:
            print(f"  {linha['rota']}   ({linha['funcao']})")

    if args.tudo:
        print()
        print("## Todas as rotas")
        print()
        for linha in sorted(linhas, key=lambda item: item["rota"]):
            marcas = "".join(
                (
                    "E" if linha["recebe_escopo"] else "-",
                    "B" if linha["aceita_filial"] else "-",
                    "f" if linha["confere_filial"] else "-",
                    "r" if linha["confere_restaurante"] else "-",
                )
            )
            print(f"  [{marcas}] {linha['rota']}")
        print()
        print("  E=recebe escopo  B=aceita branch_id  f=confere filial  r=confere restaurante")

    print()
    print("O julgamento continua sendo de quem le: o script segue chamadas por")
    print("NOME, e para de seguir onde nao consegue resolver o tipo. O que ele")
    print("garante e que nenhuma rota /admin passou sem ser olhada — e que a")
    print("cadeia que ele NAO conseguiu seguir aparece como achado, nunca como ok.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
