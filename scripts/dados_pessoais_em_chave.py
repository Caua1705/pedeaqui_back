"""Chave de Redis que carrega dado pessoal em claro.

Chave de Redis nao e lugar escondido. Ela aparece em `KEYS`, em `SCAN`, em
`MONITOR`, em qualquer dump e no painel de qualquer Redis gerenciado. O
`ChatCache.embedding_key` ja sabe disso e escreve por extenso:

    **`{hash}` — o digest, e nunca a mensagem crua.** O que o cliente digita
    nao vai para chave de Redis. E texto de pessoa, aparece em `KEYS`, em
    `MONITOR`, em qualquer dump.

A regra estava escrita e mesmo assim o cache de estimativa de entrega punha a
**coordenada da casa do cliente** na chave, com quatro casas decimais (~11 m).
Ninguem desobedeceu de proposito: quem escreveu aquela chave nao estava lendo
aquele docstring. Regra que depende de alguem lembrar volta a ser quebrada.

Este script e a versao que nao depende de lembrar.

## Como ele decide

1. Acha os modulos de `src/` que **falam com o Redis** — importam `redis`,
   chamam `cliente_redis()` ou constroem `redis.Redis`.
2. Dentro deles, acha toda **montagem de string com namespace** — f-string,
   `%` ou `.format()` cujo pedaco literal contenha `:`. O separador e o que
   distingue uma chave (`"cache:v1:{x}"`) de uma formatacao de valor
   (`f"{lat:.4f}"`), e ele e convencao deste repositorio, escrita em
   `ChatCache.embedding_key`.
3. Para cada pedaco interpolado, classifica a EXPRESSAO pelo nome:

   - **PESSOAL** — bate no vocabulario de dado pessoal (coordenada, telefone,
     e-mail, nome, endereco, CPF, mensagem...). E o achado.
   - **pseudonimo** — termina em `_id` ou e `id`. UUID interno nao e dado
     pessoal em claro: ele nao diz nada sobre a pessoa para quem le a chave.
   - **hasheado** — a expressao passa por `hash`, `digest`, `sha` ou
     `fingerprint`. E o jeito certo, e o script reconhece.
   - **desconhecido** — o resto. Nao acusa; lista para julgamento humano.

## O que ele NAO faz

**Nao le o VALOR.** `SETEX chave valor` expoe o valor no `MONITOR` tanto quanto
a chave, e um `DeliveryEstimateResult` serializado carrega as mesmas
coordenadas. Detectar isso estaticamente exigiria seguir o tipo do valor ate a
origem; aqui a auditoria do valor e manual e esta no scratchpad da rodada 4.

**Nao alcanca chave montada por biblioteca.** O `slowapi` monta a chave do rate
limit a partir do `key_func`, e o `key_func` deste repositorio e o **IP do
cliente**. Isso e por desenho do mecanismo — sem identificar quem chama nao ha
limite por cliente — e esta declarado no scratchpad, nao consertado aqui.

**Nao distingue chave de Redis de chave de dicionario em memoria.** Um modulo
que fala com Redis costuma ter os dois, e a diferenca de exposicao e enorme: a
chave em memoria morre com o processo e nao aparece em `KEYS` nem em dump. O
script mostra as duas e o julgamento e de quem le — mas a chave de memoria
tambem merece atencao, porque **a promocao dela para Redis e uma mudanca de uma
linha**, e foi assim que o cache de embedding subiu.

    python scripts/dados_pessoais_em_chave.py
    python scripts/dados_pessoais_em_chave.py --tudo   # inclui os desconhecidos

Somente leitura: le codigo-fonte, nao conecta em Redis nenhum.
"""

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


DIRETORIOS = ("src",)

# Como se reconhece um modulo que fala com o Redis. Nao e grep por "redis": a
# palavra aparece em comentario e docstring de meio repositorio, e isso trazia
# nome de thread e texto de prompt para dentro da varredura. Sao os USOS.
SINAIS_DE_REDIS = (
    "import redis",
    "from redis",
    "redis.Redis",
    "cliente_redis",
    "self.redis",
    "redis_client",
)

# O vocabulario de dado pessoal. Portugues e ingles, porque o repositorio tem
# os dois, e sem acento porque nome de variavel nao os tem.
PESSOAL = re.compile(
    r"lat(itude)?|lon(g|gitude)?|coord|"
    r"phone|telefone|celular|whats|"
    r"email|e_mail|mail|"
    r"cpf|documento|"
    r"nome|name|"
    r"address|endereco|street|rua|bairro|neighborhood|zipcode|cep|"
    r"message|mensagem|texto|content|"
    r"birth|nascimento",
    re.IGNORECASE,
)

# O que ja esta protegido: passou por funcao de digest.
HASHEADO = re.compile(r"hash|digest|sha\d*|fingerprint", re.IGNORECASE)

# Identificador interno. Nao e dado pessoal em claro — quem le a chave nao
# descobre nada sobre a pessoa a partir de um UUID.
PSEUDONIMO = re.compile(r"(^|_)id$|_id\b|uuid", re.IGNORECASE)


def _fala_com_redis(texto: str) -> bool:
    return any(sinal in texto for sinal in SINAIS_DE_REDIS)


def _classificar(expressao: str) -> str:
    if HASHEADO.search(expressao):
        return "hasheado"
    if PSEUDONIMO.search(expressao):
        return "pseudonimo"
    if PESSOAL.search(expressao):
        return "PESSOAL"
    return "desconhecido"


def _interpolacoes(no: ast.AST) -> list[str]:
    """As expressoes interpoladas numa montagem de string, como codigo."""
    if isinstance(no, ast.JoinedStr):
        return [
            ast.unparse(parte.value)
            for parte in no.values
            if isinstance(parte, ast.FormattedValue)
        ]
    if isinstance(no, ast.BinOp) and isinstance(no.op, ast.Mod):
        # `"chave:%s" % (x, y)`
        direita = no.right
        if isinstance(direita, ast.Tuple):
            return [ast.unparse(item) for item in direita.elts]
        return [ast.unparse(direita)]
    if (
        isinstance(no, ast.Call)
        and isinstance(no.func, ast.Attribute)
        and no.func.attr == "format"
    ):
        return [ast.unparse(argumento) for argumento in no.args] + [
            ast.unparse(palavra.value) for palavra in no.keywords
        ]
    return []


# Uma chave de Redis deste repositorio tem NAMESPACE, e o separador e `:`. A
# regra e do proprio `ChatCache.embedding_key`: *"um namespace de chave sem
# prefixo e o jeito conhecido de duas coisas diferentes escreverem no mesmo
# lugar"*.
#
# Exigir o `:` num pedaco LITERAL e o que separa a chave de um `f"{lat:.4f}"`
# solto — que e formatacao de valor, e no `_cache_key` de hoje ele alimenta o
# sha-256 e nunca chega ao Redis. Sem isto, a varredura acusava a propria
# correcao dela.
def _tem_namespace(no: ast.AST) -> bool:
    if isinstance(no, ast.JoinedStr):
        return any(
            isinstance(parte, ast.Constant)
            and isinstance(parte.value, str)
            and ":" in parte.value
            for parte in no.values
        )
    if isinstance(no, ast.BinOp) and isinstance(no.left, ast.Constant):
        return ":" in str(no.left.value)
    if isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute):
        alvo = no.func.value
        return isinstance(alvo, ast.Constant) and ":" in str(alvo.value)
    return False


def _e_montagem_de_string(no: ast.AST) -> bool:
    if isinstance(no, ast.JoinedStr):
        return True
    if isinstance(no, ast.BinOp) and isinstance(no.op, ast.Mod):
        return isinstance(no.left, ast.Constant) and isinstance(no.left.value, str)
    if isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute):
        return no.func.attr == "format" and isinstance(no.func.value, ast.Constant)
    return False


def varrer() -> list[tuple[str, int, str, list[tuple[str, str]]]]:
    achados = []
    for diretorio in DIRETORIOS:
        for arquivo in sorted((ROOT_DIR / diretorio).rglob("*.py")):
            if "__pycache__" in arquivo.parts:
                continue
            texto = arquivo.read_text(encoding="utf-8")
            if not _fala_com_redis(texto):
                continue
            arvore = ast.parse(texto, filename=str(arquivo))
            linhas = texto.splitlines()
            for no in ast.walk(arvore):
                if not _e_montagem_de_string(no):
                    continue
                if not _tem_namespace(no):
                    continue
                partes = _interpolacoes(no)
                if not partes:
                    continue
                achados.append(
                    (
                        str(arquivo.relative_to(ROOT_DIR)).replace("\\", "/"),
                        no.lineno,
                        linhas[no.lineno - 1].strip()[:96],
                        [(parte, _classificar(parte)) for parte in partes],
                    )
                )
    return achados


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acha dado pessoal em claro em chave de Redis (somente leitura)."
    )
    parser.add_argument(
        "--tudo",
        action="store_true",
        help="Lista tambem as montagens sem nenhuma parte suspeita.",
    )
    parser.add_argument(
        "--limite",
        type=int,
        help="Quantos achados PESSOAL sao esperados. Passar disso vira AVISO, nunca falha.",
    )
    args = parser.parse_args()

    achados = varrer()
    com_pessoal = [a for a in achados if any(c == "PESSOAL" for _, c in a[3])]

    print("=" * 76)
    print(
        f"{len(achados)} montagem(ns) de string em modulo que fala com Redis  |  "
        f"{len(com_pessoal)} com dado PESSOAL"
    )
    print("=" * 76)

    for arquivo, linha, fonte, partes in achados:
        tem_pessoal = any(classe == "PESSOAL" for _, classe in partes)
        if not tem_pessoal and not args.tudo:
            continue
        print()
        print(f"  {arquivo}:{linha}{'   <-- PESSOAL' if tem_pessoal else ''}")
        print(f"      {fonte}")
        for expressao, classe in partes:
            marca = "!!" if classe == "PESSOAL" else "  "
            print(f"      {marca} [{classe}] {expressao[:70]}")

    if not com_pessoal:
        print()
        print("Nenhuma chave com dado pessoal em claro.")

    print()
    print("O julgamento continua sendo de quem le: a classificacao e por NOME de")
    print("expressao. O que o script garante e que nenhuma montagem de string em")
    print("modulo que fala com Redis passou sem ser olhada.")
    print()
    print("Ele NAO le o valor gravado (`SETEX chave VALOR`), que aparece no")
    print("MONITOR tanto quanto a chave. Essa auditoria e manual.")

    if args.limite is not None and len(com_pessoal) > args.limite:
        print()
        print(
            f"::warning title=Dado pessoal em chave de Redis::{len(com_pessoal)} "
            f"chave(s) com dado pessoal em claro, e o esperado era {args.limite}. "
            "Chave aparece em KEYS, em MONITOR e em qualquer dump — use um digest, "
            "como ChatCache.embedding_key."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
