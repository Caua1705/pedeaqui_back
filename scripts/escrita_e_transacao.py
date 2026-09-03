"""Quem escreve, quem commita, e se as duas coisas cabem na mesma transacao.

A primeira convencao do CLAUDE.md e da skill diz:

    `endpoint -> service -> repository -> banco`. O repositorio **so**
    consulta: nao decide, nao levanta erro de regra, nao commita. Quem commita
    e o service, **sempre** — para varias escritas cairem na mesma transacao.

As tres primeiras metades dessa regra o `grep` responde, e hoje elas estao de
pe: nenhum repositorio chama `db.commit()`, nenhum levanta `HTTPException`,
nenhum endpoint importa repositorio. Este script tambem as confere, porque
custa nada e o dia em que uma delas cair tem que ser vermelho e nao descoberta.

**O que o grep NAO responde e a ultima**, e e a que custa dinheiro:

- **escrita sem commit em lugar nenhum** — a linha nunca chega ao banco. Nao
  levanta, nao loga: e um no-op silencioso, e o sintoma aparece dias depois
  como "o pedido nao registrou o cashback";
- **commit ENTRE duas escritas do mesmo metodo** — as duas deixam de ser
  atomicas. Uma falha no meio grava a primeira e perde a segunda, e no caminho
  do pedido isso e cupom consumido sem pedido, ou pedido sem historico.

As duas precisam de ORDEM e de cadeia de chamadas, e nenhuma cabe num grep.

## Como ele classifica uma chamada de repositorio

Por vocabulario de PREFIXO, e explicito nos dois lados. O que nao casa com
nenhum dos dois vira **"nao classifiquei"** e sai como achado — nunca como
leitura. Um metodo de nome novo que escrevesse e fosse tratado como leitura
seria exatamente o buraco que este script existe para fechar.

## O que ele NAO consegue fazer

**Nao enxerga `flush`.** Varios repositorios fazem `db.flush()` para obter o id
antes do commit do service, e isso e correto — mas significa que "escreveu" e
"gravou" nao sao a mesma coisa aqui. O script fala de COMMIT.

**Nao segue chamada que ele nao resolve.** Reusa o indice de
`escopo_das_rotas`, que resolve `self.metodo`, `self.atributo.metodo` (pelo
`__init__`) e `Classe(db).metodo`. O resto para, e a parada e reportada.

    python scripts/escrita_e_transacao.py
    python scripts/escrita_e_transacao.py --tudo

Somente leitura: le codigo-fonte. Nao abre banco.
"""

import argparse
import ast
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.escopo_das_rotas import _Indice, _classe_do_receptor


# Prefixos de metodo de repositorio que ESCREVEM.
PREFIXOS_DE_ESCRITA = (
    "add", "create", "delete", "update", "save", "insert", "replace",
    "attach", "assign", "mark", "unset", "clear", "invalidate", "reverse",
    "complete", "reserve", "bump", "set_", "remove", "extend",
)

# Prefixos que LEEM. Listados, e nao deduzidos por exclusao: e a exclusao que
# transforma um nome novo em leitura por acidente.
PREFIXOS_DE_LEITURA = (
    "get", "list", "count", "find", "exists", "has", "is_", "search", "latest",
    "lock", "active_", "product_ids_", "claimed_", "customer_has_",
    "last_order_", "segment_of_", "sales_", "totals_", "top_", "custo_",
    "cancellation", "rating_", "problem_tag_", "available_",
    "similarity_", "sellable_",
)

# O indice deste script e MAIOR que o de `escopo_das_rotas`. La a pergunta
# comeca numa rota, entao `src/api/endpoints` + `src/services` fecha. Aqui a
# pergunta e "alguem commita?", e quem commita as vezes mora FORA da web:
# `scripts/expire_cashback.py` chama `CashbackService.expire_balance` e commita
# no laco, uma vez por pessoa. Sem `scripts/` no indice, `expire_balance`
# apareceria como escrita sem commit — e o commit existe, so nao esta num
# service.
DIRETORIOS = ("src/api/endpoints", "src/services", "src/ai", "scripts")

# Como se reconhece o repositorio e o commit.
SUFIXO_DE_REPOSITORIO = "_repository"
METODO_DE_COMMIT = "commit"


def _e_repositorio(no: ast.Call) -> bool:
    alvo = no.func
    return (
        isinstance(alvo, ast.Attribute)
        and isinstance(alvo.value, ast.Attribute)
        and isinstance(alvo.value.value, ast.Name)
        and alvo.value.value.id == "self"
        and alvo.value.attr.endswith(SUFIXO_DE_REPOSITORIO)
    )


def _e_commit(no: ast.Call) -> bool:
    alvo = no.func
    if not isinstance(alvo, ast.Attribute) or alvo.attr != METODO_DE_COMMIT:
        return False
    receptor = alvo.value
    return (
        isinstance(receptor, ast.Attribute)
        and isinstance(receptor.value, ast.Name)
        and receptor.value.id == "self"
        and receptor.attr == "db"
    )


def _classificar(nome: str) -> str:
    if nome.startswith(PREFIXOS_DE_ESCRITA):
        return "escrita"
    if nome.startswith(PREFIXOS_DE_LEITURA):
        return "leitura"
    return "nao classifiquei"


def _eventos(corpo: ast.FunctionDef) -> list[tuple[str, str, int]]:
    """A sequencia de (tipo, nome, linha) NA ORDEM do codigo.

    `ast.walk` nao serve aqui: ele nao preserva ordem, e a pergunta do check 2
    e exatamente "o commit veio ENTRE as duas escritas?".
    """
    achados: list[tuple[str, str, int]] = []
    for no in ast.walk(corpo):
        if not isinstance(no, ast.Call):
            continue
        if _e_commit(no):
            achados.append(("commit", "db.commit", no.lineno))
        elif _e_repositorio(no):
            nome = no.func.attr
            achados.append((_classificar(nome), nome, no.lineno))
    return sorted(achados, key=lambda evento: evento[2])


def _commita_na_cadeia(
    corpo: ast.FunctionDef,
    classe: str | None,
    indice: _Indice,
    profundidade: int,
    visitados: set,
) -> tuple[bool, bool]:
    """(commita em algum lugar, seguiu tudo o que viu)."""
    if profundidade > 4:
        return False, False
    commita = False
    completo = True
    for no in ast.walk(corpo):
        if not isinstance(no, ast.Call):
            continue
        if _e_commit(no):
            commita = True
        alvo = _classe_do_receptor(no, classe, indice)
        nome = no.func.attr if isinstance(no.func, ast.Attribute) else None
        if alvo is None or nome is None:
            continue
        proximo = indice.metodos.get((alvo, nome))
        if proximo is None or (alvo, nome) in visitados:
            continue
        visitados.add((alvo, nome))
        c, ok = _commita_na_cadeia(proximo, alvo, indice, profundidade + 1, visitados)
        commita = commita or c
        completo = completo and ok
    return commita, completo


def _chamados_por_alguem(indice: _Indice) -> set:
    """Quem e CHAMADO por outro metodo do indice.

    O check do commit so faz sentido nos PONTOS DE ENTRADA. Um helper privado
    como `CustomerAnonymizationService._delete_addresses` escreve e nao
    commita — e esta certo: quem commita e o `anonymize` que o chama, uma vez,
    depois de todos os passos. Cobrar commit dele seria pedir exatamente o
    contrario do que a regra manda ("varias escritas na MESMA transacao").
    """
    chamados = set()
    # `.funcoes` TAMBEM, e nao so `.metodos`: quem chama `expire_balance` e
    # `_expirar`, uma funcao de modulo em `scripts/expire_cashback.py`. Olhar
    # so para metodos deixaria de fora todo chamador que nao esta numa classe —
    # os scripts de manutencao inteiros.
    for (classe, _), corpo in list(indice.metodos.items()) + list(indice.funcoes.items()):
        for no in ast.walk(corpo):
            if not isinstance(no, ast.Call):
                continue
            alvo = _classe_do_receptor(no, classe, indice)
            nome = no.func.attr if isinstance(no.func, ast.Attribute) else None
            if alvo is not None and nome is not None:
                chamados.add((alvo, nome))
    return chamados


def auditar(raiz: Path | None = None) -> dict[str, list]:
    """`raiz` existe para o teste montar uma arvore PLANTADA.

    Sem ela nao ha como provar que este varredor acusa o que deve acusar — e
    varredor visto so respondendo "nenhum" nao provou nada. E a mesma razao do
    parametro `diretorios` de `_Indice`.
    """
    raiz = raiz or ROOT_DIR
    indice = _Indice([raiz / d for d in DIRETORIOS])
    chamados = _chamados_por_alguem(indice)
    sem_commit, commit_no_meio, nao_classificados = [], [], []

    for (classe, metodo), corpo in sorted(indice.metodos.items()):
        eventos = _eventos(corpo)
        escritas = [evento for evento in eventos if evento[0] == "escrita"]
        nao_classificados.extend(
            (classe, metodo, nome, linha)
            for tipo, nome, linha in eventos
            if tipo == "nao classifiquei"
        )
        if not escritas:
            continue

        # Check 2: commit ENTRE duas escritas, na ordem do codigo. A linha
        # reportada e a do COMMIT — e ela que esta no lugar errado.
        relevantes = [e for e in eventos if e[0] in ("escrita", "commit")]
        for i in range(len(relevantes) - 2):
            trio = [e[0] for e in relevantes[i : i + 3]]
            if trio == ["escrita", "commit", "escrita"]:
                commit_no_meio.append((classe, metodo, relevantes[i + 1][2]))
                break

        # Check 1: alguma coisa na cadeia commita? So para PONTO DE ENTRADA —
        # helper chamado por outro metodo herda o commit de quem o chama.
        if (classe, metodo) in chamados:
            continue
        commita, _ = _commita_na_cadeia(corpo, classe, indice, 0, set())
        if not commita:
            sem_commit.append((classe, metodo, escritas[0][1], escritas[0][2]))

    return {
        "sem_commit": sem_commit,
        "commit_no_meio": commit_no_meio,
        "nao_classificados": sorted(set(nao_classificados)),
        "repositorio_commita": _repositorios_que(r"self\.db\.commit\(\)", raiz),
        "repositorio_levanta": _repositorios_que("HTTPException", raiz),
        "endpoint_usa_repositorio": _endpoints_com_repositorio(raiz),
    }


def _repositorios_que(marca: str, raiz: Path) -> list[str]:
    import re

    achados = []
    for arquivo in sorted((raiz / "src/repositories").rglob("*.py")):
        texto = arquivo.read_text(encoding="utf-8")
        for numero, linha in enumerate(texto.splitlines(), start=1):
            if linha.lstrip().startswith("#") or re.search(marca, linha) is None:
                continue
            achados.append(f"{arquivo.name}:{numero}")
    return achados


def _endpoints_com_repositorio(raiz: Path) -> list[str]:
    achados = []
    for arquivo in sorted((raiz / "src/api/endpoints").rglob("*.py")):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
        for no in ast.walk(arvore):
            if isinstance(no, ast.ImportFrom) and (no.module or "").startswith(
                "src.repositories"
            ):
                achados.append(f"{arquivo.name}:{no.lineno}")
    return achados


TITULOS = {
    "sem_commit": "Service que ESCREVE e nao commita em lugar nenhum da cadeia",
    "commit_no_meio": "Service que commita ENTRE duas escritas (as duas deixam de ser atomicas)",
    "repositorio_commita": "Repositorio que commita (quem commita e o service)",
    "repositorio_levanta": "Repositorio que levanta HTTPException (regra e do service)",
    "endpoint_usa_repositorio": "Endpoint que importa repositorio (`endpoint -> repository` nao existe)",
    "nao_classificados": "Metodo de repositorio que eu nao soube dizer se le ou escreve",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audita escrita, commit e camadas (somente leitura)."
    )
    parser.add_argument("--tudo", action="store_true")
    args = parser.parse_args()

    encontrados = auditar()
    total = sum(len(v) for v in encontrados.values())

    print("=" * 78)
    print(f"{total} achado(s)")
    print("=" * 78)

    for chave, titulo in TITULOS.items():
        grupo = encontrados[chave]
        if not grupo and not args.tudo:
            print()
            print(f"## {titulo}  (0)")
            print()
            print("  Nenhum.")
            continue
        print()
        print(f"## {titulo}  ({len(grupo)})")
        print()
        if not grupo:
            print("  Nenhum.")
        for item in grupo:
            print(f"  {item if isinstance(item, str) else '  '.join(str(p) for p in item)}")

    print()
    print("`flush()` nao e commit: varios repositorios o usam para obter o id")
    print("antes do commit do service, e isso e correto. Este script fala de")
    print("COMMIT — o que decide se as escritas do metodo sao atomicas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
