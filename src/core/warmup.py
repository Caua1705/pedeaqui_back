"""Paga no boot o que a primeira pergunta do dia pagava.

O QUE ISTO TIRA DA FRENTE DO CLIENTE. Medido em producao em 24/08/2026, na
primeira requisicao do processo contra as seguintes:

    embedding_ms          3534,26   ->   0,39 (cache) / ~400 (mediana)
    restaurant_lookup_ms     78,57   ->  ~12
    total_ms               8158,53   ->  2171

Os 3,5 s do embedding sao o `OpenAIEmbeddings` sendo construido pelo
`lru_cache` de `embedding_service.py` e abrindo a primeira conexao com a
OpenAI — DNS, TCP e handshake TLS. Os ~66 ms do banco sao o pool abrindo a
primeira conexao com o Postgres. Nenhum dos dois se repete; os dois caem
inteiros no colo de quem perguntar primeiro, que e justamente quem tem menos
paciencia para esperar.

TRES DECISOES QUE PARECEM DETALHE:

**Falha aqui nunca derruba o boot.** E o oposto de `startup_checks`, e de
proposito: aquele recusa subir porque configuracao faltando produz uma API
que aceita pedido e nao consegue cumpri-lo. Este e desempenho. Com a OpenAI
fora do ar, a API tem que subir do mesmo jeito — o cardapio, o pedido e o
pagamento nao dependem dela, e derrubar a plataforma inteira porque o
assistente nao aqueceu seria trocar um problema pequeno por um enorme.

**Sincrono, dentro do lifespan, com teto por etapa.** O boot espera, porque
jogar tudo para segundo plano criaria uma corrida entre o aquecimento e a
primeira requisicao — que e exatamente o caso que se quer resolver. Mas
espera no MAXIMO `AI_WARMUP_TIMEOUT_SECONDS` por etapa, e desistir de esperar
nao cancela o aquecimento: ver `_with_timeout`.

**As TRES chamadas sao de verdade.** A primeira versao deste arquivo so
construia o cliente do LLM e registrava "pronto em 2.36 ms" — que era a prova
de que nada tinha sido aquecido, porque construir o `ChatOpenAI` nao fala com
a rede. A medicao seguinte mostrou o handshake inteiro ainda no primeiro
turno: 3616 ms para 85 tokens de saida contra 1744 ms para 67 no turno
seguinte, ~1,8 s que nao cabem na geracao. Hoje o LLM leva uma geracao
minima, cobrada uma vez por deploy e por worker.
"""

import logging
import threading
from time import perf_counter

from sqlalchemy import text

from src.ai.services.chat_llm_service import get_chat_client
from src.ai.services.embedding_service import get_embeddings_client
from src.core.config import settings
from src.db.session import SessionLocal


logger = logging.getLogger("uvicorn.error")

# Frase curta e sem sentido de negocio: o vetor dela e jogado fora, o que
# importa e a conexao que a chamada deixa aberta.
_FRASE_DE_AQUECIMENTO = "aquecimento"
# Curto de proposito: o que importa e a conexao que a chamada deixa aberta,
# nao a resposta. Pedir brevidade explicitamente segura o custo por deploy.
_PROMPT_DE_AQUECIMENTO = "Responda apenas: ok"
_SELECT_1 = text("SELECT 1")


def warm_up() -> None:
    """Aquece banco e OpenAI. Nunca levanta e nunca pendura o boot."""
    if not settings.AI_WARMUP_ENABLED:
        logger.info("[warmup] desligado por AI_WARMUP_ENABLED=false")
        return

    _with_timeout("banco", _open_database_connection)
    _with_timeout("embedding", _call_embeddings)
    _with_timeout("llm", _call_chat_model)


def _with_timeout(nome: str, aquecer) -> None:
    """Roda o aquecimento numa thread e desiste de ESPERAR, nao de aquecer.

    POR QUE THREAD, E NAO O `timeout` DO CLIENTE. O que precisa ser aquecido e
    o pool de conexoes do cliente COMPARTILHADO — e o `lru_cache` de
    `get_chat_client` / `get_embeddings_client` que a requisicao do cliente vai
    reusar. Um cliente descartavel com timeout proprio abriria a conexao dele,
    aqueceria o pool dele e nao serviria para nada. Configurar o timeout no
    cliente compartilhado tambem nao serve: passaria a valer para as
    requisicoes de producao, que e mudanca de comportamento sem relacao com
    boot.

    E DESISTIR DE ESPERAR E MELHOR QUE ABORTAR. A thread e `daemon` e continua
    rodando: se a chamada estava so lenta, ela termina depois do boot, o pool
    fica quente do mesmo jeito e o cliente seguinte se beneficia. Um timeout
    que CANCELA deixaria o pior dos dois mundos — boot atrasado e conexao
    ainda fria.

    O TIMEOUT E 8 s POR ETAPA (`AI_WARMUP_TIMEOUT_SECONDS`), e ele desceu de
    15 s em 24/08/2026 porque a leitura da medicao estava errada.

    O 15 s vinha de ler os 4844 ms do aquecimento do embedding como "um boot
    normal" e pedir ~3x de folga sobre ele. Mas 4844 ms **e** o caso lento: a
    unica chamada que existe no boot e a FRIA, com DNS, TCP e handshake TLS
    dentro. Nao ha um caso mais lento a que dar folga — 3x sobre o pior
    observado nao e margem, e um teto que nunca dispara.

    E DISPARAR A TOA E BARATO, o que inverte o calculo. O argumento antigo
    dizia que estourar "loga um aviso alarmante sobre algo que ia funcionar" —
    mas o desenho desta funcao e justamente o que tira o preco disso: a thread
    e daemon, nao e cancelada, e o aquecimento termina alguns segundos depois
    do boot com o pool quente do mesmo jeito. O que se perde num disparo a toa
    e uma linha de log. O que se perde em NAO disparar e o boot inteiro
    pendurado, servindo 502.

    O teto do BOOT e o que limita por cima: 3 etapas x 8 s = 24 s no caso
    patologico (eram 45 s), somados ao `alembic upgrade head` que o
    `docker-entrypoint.sh` ja roda antes do Uvicorn. Cabe porque o servico da
    API **nao tem healthcheck** no `docker-compose.yml` (so o Redis tem) e o
    `restart: always` so dispara quando o processo MORRE — boot lento vira 502
    no Traefik por alguns segundos, nunca loop de restart (armadilha 5).

    **A ausencia de healthcheck virou dependencia deste numero, e isso e a
    armadilha 40.** No dia em que a API ganhar um, o `start_period` dele tem
    que ser maior que a soma dos tres timeouts, senao o container entra em
    loop de restart durante o proprio boot.
    """
    resultado: dict[str, float] = {}

    def alvo() -> None:
        started_at = perf_counter()
        try:
            aquecer()
        except Exception:
            logger.warning("[warmup] %s nao aqueceu", nome, exc_info=True)
            return
        resultado["ms"] = (perf_counter() - started_at) * 1000

    thread = threading.Thread(target=alvo, name=f"warmup-{nome}", daemon=True)
    thread.start()
    thread.join(settings.AI_WARMUP_TIMEOUT_SECONDS)

    if thread.is_alive():
        logger.warning(
            "[warmup] %s passou de %.0f s; o boot seguiu e o aquecimento continua",
            nome,
            settings.AI_WARMUP_TIMEOUT_SECONDS,
        )
        return
    if "ms" in resultado:
        logger.info("[warmup] %s pronto em %.2f ms", nome, resultado["ms"])


def _open_database_connection() -> None:
    with SessionLocal() as db:
        db.execute(_SELECT_1)


def _call_embeddings() -> None:
    """Construir o `OpenAIEmbeddings` nao abre conexao; quem abre e o `embed_query`."""
    get_embeddings_client().embed_query(_FRASE_DE_AQUECIMENTO)


def _call_chat_model() -> None:
    """Uma geracao MINIMA de verdade — construir o objeto nao era aquecer nada.

    A primeira versao so chamava `get_chat_client(...)` e registrava
    "pronto em 2.36 ms". Os 2,36 ms eram a prova de que nada tinha sido
    aquecido: construir o `ChatOpenAI` nao fala com a rede, e o handshake
    continuava inteiro no primeiro turno. A medicao mostra o tamanho dele —
    turno 1 gastou 3616 ms para 85 tokens de saida, turno 2 gastou 1744 ms
    para 67; a diferenca nao cabe na geracao.

    O prompt e minusculo e a resposta tambem, mas o custo NAO e zero: e uma
    geracao cobrada por deploy, por worker. E o preco de o primeiro cliente
    depois de cada deploy nao pagar ~1,8 s.

    Vai pelo cliente compartilhado de proposito, e nao por um `ChatOpenAI`
    proprio: e o pool DELE que a requisicao seguinte vai reusar.
    """
    get_chat_client(settings.MODEL_NAME).invoke(_PROMPT_DE_AQUECIMENTO)
