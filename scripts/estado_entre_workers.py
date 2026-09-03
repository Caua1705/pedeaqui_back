"""Estado que mora no PROCESSO, e o que ele custa no dia de subir o segundo.

A armadilha 20 da skill diz, e ja dizia antes deste script:

    Nada em memoria sobrevive a mais de um worker.

O `Dockerfile` sobe **um** worker, entao hoje nada disto quebra. E exatamente
por isso que a classe e perigosa: ela e latente atras de UMA linha de
configuracao. No dia de escalar para o sabado cheio, tudo o que mora no
processo passa a existir num worker e nao no outro — **sem erro, sem log, e
nao-deterministico**, porque o sintoma depende de qual worker atendeu.

## Por que uma ferramenta, e nao a tabela que a regra ja tinha

A regra listava tres estruturas. Este script achou **cinco contadores** em
`chat_service.py` que nao estavam na lista, e o motivo de nao estarem e o de
sempre: a lista foi escrita a mao uma vez e o codigo andou. Lista escrita a
mao envelhece calada; varredura no portao, nao.

## O criterio NAO e "e um dicionario"

A primeira medicao perguntou "que nomes de modulo sao estrutura mutavel?" e
achou 24 — e as 24 eram tabela CONSTANTE (`PAYMENT_METHOD_LABELS`,
`ORDER_STATUS_TRANSITIONS`). Tabela so lida e compartilhada de graca entre
workers: cada processo tem a sua, identica, e ninguem escreve.

O que quebra e **escrita em tempo de requisicao**. Entao o criterio e quem
ESCREVE:

- `global X` seguido de atribuicao — os contadores;
- `X[chave] = ...` e `del X[chave]`;
- metodo que muta (`append`, `update`, `clear`, `pop`, `add`, ...).

## As tres formas que ele procura

1. **nome de modulo escrito de dentro de uma funcao** — o contador, o cache
   caseiro, a flag de cold start;
2. **instancia no nivel do modulo** — o objeto existe uma vez por processo, e
   o que ele guardar dentro vai junto. `historico = Historico()` e
   `limiter = Limiter(...)` sao isto;
3. **funcao com cache de processo** (`lru_cache`, `cache`) — cache por worker.
   Para um CLIENTE (conexao, engine) e o uso certo e barato; para dado de
   requisicao e a armadilha inteira, com o agravante de nao ter prazo.

## Tipo inerte e lista EXPLICITA, nunca deducao por exclusao

`ZoneInfo`, `Decimal` e `APIRouter` nao guardam estado de requisicao, e sao
declarados um a um em `TIPOS_INERTES` com o motivo. Deduzir "o que eu nao
conheco deve ser inerte" transformaria um tipo NOVO em estado invisivel — que
e o buraco que este script existe para fechar. O que ele nao reconhece vira
achado.

`ESPERADOS` lista o que e estado de processo **de proposito**, com o motivo e
o que acontece com N workers. Sitio novo fora dela e achado.

    python scripts/estado_entre_workers.py
    python scripts/estado_entre_workers.py --tudo

Somente leitura: le codigo-fonte. Nao abre banco.
"""

import argparse
import ast
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]

DIRETORIOS = ("src",)

# Metodos que ESCREVEM no objeto. Lista explicita: um metodo mutante novo tem
# que ser acrescentado aqui, e nao deduzido por nao estar numa lista de
# leitores.
METODOS_QUE_MUTAM = frozenset(
    {
        "append", "extend", "insert", "pop", "remove", "clear", "update",
        "setdefault", "add", "discard", "popitem", "sort", "appendleft",
        "popleft", "cache_clear",
    }
)

# Decoradores que guardam resultado NO PROCESSO.
DECORADORES_DE_CACHE = ("lru_cache", "cache", "cached_property")

# Tipos que nao guardam estado de requisicao, um a um e com o motivo.
TIPOS_INERTES = {
    "APIRouter": "tabela de rotas, montada no import e nunca escrita depois",
    "Decimal": "valor imutavel",
    "ZoneInfo": "fuso, imutavel",
    "TypeVar": "anotacao de tipo",
    "Query": "declaracao de parametro do FastAPI, lida na assinatura",
    "HTTPBearer": "esquema de seguranca, so configuracao",
    "CryptContext": "configuracao do passlib; nao guarda nada por requisicao",
    "getLogger": "logger do stdlib; o estado dele e a configuracao, nao a requisicao",
    "compile": "regex compilado, imutavel",
    "frozenset": "imutavel",
    "tuple": "imutavel",
    "timedelta": "imutavel",
    "date": "imutavel",
    "datetime": "imutavel",
    "text": "clausula SQL do SQLAlchemy, imutavel",
    "replace": "`Decimal.replace`/`date.replace` devolvem valor novo e imutavel",
}

# Estado de processo DE PROPOSITO. A chave e `arquivo:nome`, e o valor diz o
# que acontece com N workers — que e a pergunta que a armadilha 20 faz.
ESPERADOS = {
    "src/api/rate_limit.py:limiter": (
        "contadores do rate limit. Com N workers e SEM `REDIS_URL`, o limite "
        "efetivo vira N x o configurado. `REDIS_URL` resolve sem tocar em "
        "codigo, e `startup_checks` avisa no boot quando `--workers > 1` sem ela"
    ),
    "src/ai/services/chat_history.py:historico": (
        "fachada do historico do chat. Com `REDIS_URL` o backend e o Redis e "
        "nada fica no processo; sem ela, cai no de memoria — que e o certo na "
        "bancada e na suite"
    ),
    "src/ai/services/chat_cache.py:chat_cache": (
        "cache de resposta e de embedding. Vive no Redis quando ha `REDIS_URL`; "
        "sem ela o custo e chamada repetida ao modelo, nao resposta errada"
    ),
    "src/ai/services/chat_cache.py:menu_generation": (
        "mesmo caso do `chat_cache`, para a geracao de cardapio"
    ),
    "src/integrations/google_identity_client.py:CHAVES_DO_GOOGLE": (
        "cache do JWKS do Google — as chaves PUBLICAS com que ele assina os "
        "`id_token`. Com N workers sao N copias das mesmas chaves publicas, e "
        "isso e o CERTO: nao ha nada a compartilhar, cada copia se renova "
        "sozinha pelo TTL, e a rotacao de chave e coberta pela rebusca por "
        "`kid` desconhecido — nao pelo TTL. O custo de N copias e uma chamada "
        "por hora por worker a um endereco publico e cacheado"
    ),
    "src/core/config.py:settings": (
        "configuracao lida do ambiente no import. Igual em todo worker, porque "
        "o ambiente e o mesmo — e nada a escreve em tempo de requisicao (o "
        "grupo `escrito` acima e quem cobra isso)"
    ),
    "src/services/chat_service.py:_cold_start_pending": (
        "flag de UMA vez por processo, para o log dizer que aquele turno pagou "
        "o primeiro carregamento. Com N workers sao N linhas de cold start, e "
        "isso e o CERTO: cada processo tem o seu"
    ),
    "src/services/chat_service.py:_resgates_por_nome": (
        "contador de diagnostico do log. Com N workers cada um conta a SUA "
        "fatia; a razao que o log imprime continua valendo por worker, e o "
        "numero absoluto nao e usado para nada que decida"
    ),
    "src/services/chat_service.py:_turnos_com_llm": "par do `_resgates_por_nome`",
    "src/services/chat_service.py:_turnos_com_contexto": (
        "contador de diagnostico do log, mesmo caso do `_resgates_por_nome`"
    ),
    "src/services/chat_service.py:_turnos_sem_cartao": "par do `_turnos_com_contexto`",
    "src/ai/services/chat_cache.py:cliente_redis": (
        "CLIENTE, e nao dado: um pool de conexao por processo e o uso certo do "
        "`lru_cache`. Sem ele, cada chamada abriria conexao nova"
    ),
    "src/ai/services/chat_llm_service.py:get_chat_client": "cliente da OpenAI, mesmo caso",
    "src/ai/services/embedding_service.py:get_embeddings_client": "cliente de embedding, mesmo caso",
    "src/core/config.py:get_settings": (
        "configuracao lida do ambiente uma vez. Igual em todo worker, porque o "
        "ambiente e o mesmo"
    ),
    "src/db/session.py:get_engine": (
        "engine do SQLAlchemy, que JA e um pool. Um por processo e o desenho "
        "dele; a construcao tardia existe para a suite poder apontar para outro "
        "banco depois do import"
    ),
}


class Achado:
    def __init__(self, forma: str, arquivo: str, nome: str, linha: int, detalhe: str = ""):
        self.forma = forma
        self.arquivo = arquivo
        self.nome = nome
        self.linha = linha
        self.detalhe = detalhe

    @property
    def chave(self) -> str:
        return f"{self.arquivo}:{self.nome}"

    def __str__(self) -> str:
        extra = f"  ({self.detalhe})" if self.detalhe else ""
        return f"{self.arquivo}:{self.linha}  {self.nome}{extra}"


def _nomes_do_modulo(arvore: ast.Module) -> dict[str, ast.AST]:
    nomes: dict[str, ast.AST] = {}
    for no in arvore.body:
        if isinstance(no, ast.Assign):
            for alvo in no.targets:
                if isinstance(alvo, ast.Name):
                    nomes[alvo.id] = no
        elif isinstance(no, ast.AnnAssign) and isinstance(no.target, ast.Name):
            nomes[no.target.id] = no
    return nomes


def _escritas(no_da_funcao: ast.AST, nomes: set[str]) -> list[tuple[str, int, str]]:
    """(nome, linha, como) de toda escrita em nome de modulo, dentro da funcao."""
    achadas: list[tuple[str, int, str]] = []
    declarados_global: set[str] = set()

    for interno in ast.walk(no_da_funcao):
        if isinstance(interno, ast.Global):
            declarados_global.update(interno.names)

    for interno in ast.walk(no_da_funcao):
        if isinstance(interno, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            alvos = interno.targets if isinstance(interno, ast.Assign) else [interno.target]
            for alvo in alvos:
                # `global X` + `X = ...` / `X += ...`
                if isinstance(alvo, ast.Name) and alvo.id in declarados_global:
                    achadas.append((alvo.id, interno.lineno, "global + atribuicao"))
                # `X[chave] = ...`
                elif isinstance(alvo, ast.Subscript) and isinstance(alvo.value, ast.Name):
                    if alvo.value.id in nomes:
                        achadas.append((alvo.value.id, interno.lineno, "escrita por chave"))
                # `X.campo = ...` — o `settings.x = y` e o
                # `singleton.contador += 1` que nenhum dos dois casos acima
                # pega, e que sao estado de processo do mesmo jeito.
                elif isinstance(alvo, ast.Attribute) and isinstance(alvo.value, ast.Name):
                    if alvo.value.id in nomes:
                        achadas.append((alvo.value.id, interno.lineno, "escrita por atributo"))
        elif isinstance(interno, ast.Delete):
            for alvo in interno.targets:
                if isinstance(alvo, ast.Subscript) and isinstance(alvo.value, ast.Name):
                    if alvo.value.id in nomes:
                        achadas.append((alvo.value.id, interno.lineno, "del por chave"))
        elif isinstance(interno, ast.Call) and isinstance(interno.func, ast.Attribute):
            receptor = interno.func.value
            if (
                interno.func.attr in METODOS_QUE_MUTAM
                and isinstance(receptor, ast.Name)
                and receptor.id in nomes
            ):
                achadas.append((receptor.id, interno.lineno, f"{interno.func.attr}()"))
    return achadas


def _tipos_congelados(raiz: Path) -> set[str]:
    """Classes do repositorio que nao tem como guardar estado: `frozen=True`,
    `NamedTuple`, `Enum`.

    Regra MECANICA, e nao lista escrita a mao: `CustomerListFilters`,
    `_PadraoDoRestaurante` e `CashbackTerms` sao sentinelas congeladas, e
    listar as tres seria dar manutencao numa lista que envelhece. Uma classe
    congelada nova entra sozinha; uma que DEIXAR de ser congelada volta a ser
    achado, que e o que se quer.
    """
    congelados: set[str] = set()
    for diretorio in DIRETORIOS:
        for arquivo in (raiz / diretorio).rglob("*.py"):
            if "__pycache__" in arquivo.parts:
                continue
            for no in ast.walk(ast.parse(arquivo.read_text(encoding="utf-8"))):
                if not isinstance(no, ast.ClassDef):
                    continue
                decoradores = " ".join(ast.unparse(d) for d in no.decorator_list)
                bases = " ".join(ast.unparse(b) for b in no.bases)
                if "frozen=True" in decoradores or "NamedTuple" in bases or "Enum" in bases:
                    congelados.add(no.name)
    return congelados


def _tipo_instanciado(no: ast.Assign) -> str | None:
    chamada = no.value
    if not isinstance(chamada, ast.Call):
        return None
    alvo = chamada.func
    if isinstance(alvo, ast.Name):
        return alvo.id
    if isinstance(alvo, ast.Attribute):
        return alvo.attr
    return None


def auditar(raiz: Path | None = None) -> dict[str, list]:
    """`raiz` existe para o teste montar uma arvore PLANTADA — mesmo motivo do
    `raiz` de `escrita_e_transacao` e do `diretorios` de `_Indice`."""
    raiz = raiz or ROOT_DIR
    inertes = set(TIPOS_INERTES) | _tipos_congelados(raiz)
    escritos: list[Achado] = []
    instancias: list[Achado] = []
    caches: list[Achado] = []

    for diretorio in DIRETORIOS:
        for arquivo in sorted((raiz / diretorio).rglob("*.py")):
            if "__pycache__" in arquivo.parts:
                continue
            relativo = arquivo.relative_to(raiz).as_posix()
            arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
            nomes = _nomes_do_modulo(arvore)

            vistos: set[tuple[str, str]] = set()
            for no in ast.walk(arvore):
                if not isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for nome, linha, como in _escritas(no, set(nomes)):
                    if (relativo, nome) in vistos:
                        continue
                    vistos.add((relativo, nome))
                    escritos.append(Achado("escrito", relativo, nome, linha, como))

                for decorador in no.decorator_list:
                    texto = ast.unparse(decorador)
                    if any(marca in texto for marca in DECORADORES_DE_CACHE):
                        caches.append(Achado("cache", relativo, no.name, no.lineno, texto))

            for no in arvore.body:
                if not isinstance(no, ast.Assign) or not isinstance(no.targets[0], ast.Name):
                    continue
                tipo = _tipo_instanciado(no)
                if tipo is None or tipo in inertes:
                    continue
                nome = no.targets[0].id
                instancias.append(Achado("instancia", relativo, nome, no.lineno, f"{tipo}(...)"))

    def fora_da_lista(grupo: list[Achado]) -> list[Achado]:
        return [achado for achado in grupo if achado.chave not in ESPERADOS]

    return {
        "escritos": fora_da_lista(escritos),
        "instancias": fora_da_lista(instancias),
        "caches": fora_da_lista(caches),
        "declarados": [
            chave
            for chave in ESPERADOS
            if chave not in {a.chave for a in escritos + instancias + caches}
        ],
    }


TITULOS = {
    "escritos": "Nome de modulo ESCRITO em tempo de requisicao",
    "instancias": "Instancia no nivel do modulo, de tipo que nao sei ser inerte",
    "caches": "Funcao com cache de PROCESSO",
    "declarados": "Declarado em ESPERADOS e nao encontrado no codigo (lista velha)",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acha estado que mora no processo (somente leitura)."
    )
    parser.add_argument("--tudo", action="store_true", help="lista tambem o que esta em ESPERADOS")
    args = parser.parse_args()

    encontrados = auditar()
    total = sum(len(grupo) for grupo in encontrados.values())

    print("=" * 78)
    print(f"{total} achado(s)  |  {len(ESPERADOS)} sitio(s) declarados em ESPERADOS")
    print("=" * 78)

    for chave, titulo in TITULOS.items():
        grupo = encontrados[chave]
        print()
        print(f"## {titulo}  ({len(grupo)})")
        print()
        if not grupo:
            print("  Nenhum.")
        for item in grupo:
            print(f"  {item}")

    if args.tudo:
        print()
        print("## O que E estado de processo de proposito, e o que acontece com N workers")
        print()
        for chave, motivo in sorted(ESPERADOS.items()):
            print(f"  {chave}")
            print(f"      {motivo}")

    print()
    print("O `Dockerfile` sobe UM worker, entao nada disto quebra hoje — e e por")
    print("isso que a classe e perigosa: ela e latente atras de uma linha de")
    print("configuracao, e o sintoma depende de qual worker atendeu.")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
