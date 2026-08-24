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

**Sincrono, dentro do lifespan.** Sao ~3,5 s uma vez por processo, e o
`docker-entrypoint.sh` ja roda `alembic upgrade head` antes do Uvicorn — o
boot ja nao e instantaneo. Jogar isto para segundo plano economizaria
segundos no boot ao preco de uma corrida entre o aquecimento e a primeira
requisicao, que e exatamente o caso que se quer resolver.

**O cliente do LLM e so CONSTRUIDO, sem chamada.** Aquecer o TLS dele de
verdade exigiria uma geracao de verdade — dinheiro e ~2 s a cada restart,
por um ganho que a medicao nao isola. O que da para fazer de graca e tirar a
construcao do objeto do caminho quente, e e o que se faz.
"""

import logging
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
_SELECT_1 = text("SELECT 1")


def warm_up() -> None:
    """Aquece banco e OpenAI. Nunca levanta."""
    if not settings.AI_WARMUP_ENABLED:
        logger.info("[warmup] desligado por AI_WARMUP_ENABLED=false")
        return

    _warm_up_database()
    _warm_up_embeddings()
    _warm_up_chat_client()


def _warm_up_database() -> None:
    started_at = perf_counter()
    try:
        with SessionLocal() as db:
            db.execute(_SELECT_1)
    except Exception:
        logger.warning("[warmup] banco nao aqueceu", exc_info=True)
        return
    logger.info("[warmup] banco pronto em %.2f ms", (perf_counter() - started_at) * 1000)


def _warm_up_embeddings() -> None:
    """A chamada de verdade — e a unica que precisa ser de verdade.

    Construir o `OpenAIEmbeddings` nao abre conexao; quem abre e o
    `embed_query`. Sem uma chamada real, o `lru_cache` guardaria um cliente
    que ainda pagaria DNS e TLS na primeira pergunta, e os 3,5 s
    continuariam no lugar onde estao hoje.
    """
    started_at = perf_counter()
    try:
        get_embeddings_client().embed_query(_FRASE_DE_AQUECIMENTO)
    except Exception:
        logger.warning("[warmup] embedding nao aqueceu", exc_info=True)
        return
    logger.info(
        "[warmup] embedding pronto em %.2f ms", (perf_counter() - started_at) * 1000
    )


def _warm_up_chat_client() -> None:
    started_at = perf_counter()
    try:
        get_chat_client(settings.MODEL_NAME)
    except Exception:
        logger.warning("[warmup] cliente do LLM nao aqueceu", exc_info=True)
        return
    logger.info(
        "[warmup] cliente do LLM pronto em %.2f ms",
        (perf_counter() - started_at) * 1000,
    )
