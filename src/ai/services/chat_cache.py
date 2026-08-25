import hashlib
import logging
import re
import struct
import threading
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from time import monotonic
from typing import Any, Generic, TypeVar

from src.core.config import settings


logger = logging.getLogger("uvicorn.error")

T = TypeVar("T")
_EMBEDDING_TTL_SECONDS = 60 * 60
_RETRIEVAL_TTL_SECONDS = 20 * 60

# Versao do FORMATO da chave do embedding. Ver `ChatCache.embedding_key`: ela
# sobe quando muda a normalizacao, o digest ou o empacotamento do vetor —
# nunca quando muda o modelo, que ja e um campo proprio da chave.
_EMBEDDING_KEY_VERSION = "v1"


@lru_cache
def cliente_redis():
    """O cliente Redis do processo. Um so, construido na primeira necessidade.

    `decode_responses=False`, e isso NAO e detalhe: o vetor do embedding e
    gravado como bytes (ver `ChatCache._empacotar`). Com `decode_responses=True`
    o `redis-py` tentaria `.decode("utf-8")` em 6 KB de float binario e
    levantaria `UnicodeDecodeError` em toda leitura — um cache que nunca
    acerta, sem nada no log dizendo por que. Quem le texto por aqui
    (`MenuGeneration.current`) decodifica na mao.

    `lru_cache` e nao um objeto de modulo, pelo mesmo motivo de `get_engine` em
    `src/db/session.py`: construido no import, congelaria `settings` no
    instante em que qualquer modulo de `src` fosse importado.

    Sem `REDIS_URL` devolve `None`, e todo chamador daqui trata `None` como
    "nao ha Redis" — nao como erro. Ver o docstring de `MenuGeneration`.
    """
    if not settings.REDIS_URL:
        return None
    try:
        import redis

        return redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=False,
            socket_timeout=1,
            socket_connect_timeout=1,
        )
    except Exception:
        logger.warning("[AI cache] redis_initialization_failed=true")
        return None


@dataclass
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class MenuGeneration:
    """Contador por restaurante que invalida o cache de busca quando o indice muda.

    Mora no Redis, e nao em memoria, por um motivo simples: quem MUDA o indice
    e o worker de reindex, que e outro processo, em outro container. Um
    contador em memoria nunca veria o incremento dele, e o cache de busca
    continuaria servindo o cardapio velho pelos 20 minutos do TTL — que e
    exatamente o atraso que a varredura automatica existe para eliminar.

    Sem `REDIS_URL` o contador fica parado em zero e nada quebra: o cache volta
    a ser so-TTL, que e o comportamento que ja existia antes deste arquivo
    mudar. Degradar em silencio e aceitavel aqui porque o TTL continua sendo o
    teto do erro — no pior caso o Rapi demora 20 minutos para conhecer o
    produto novo, em vez de nunca.
    """

    def __init__(self, redis_client=None) -> None:
        self._redis_injetado = redis_client

    @property
    def redis(self):
        """O cliente do processo, ou o injetado no construtor (teste).

        Passou a vir de `cliente_redis()` para que exista UM pool de conexoes
        no processo, e nao um por consumidor: o contador de geracao e o cache
        de embedding falam com o mesmo Redis, pela mesma rede.
        """
        if self._redis_injetado is not None:
            return self._redis_injetado
        return cliente_redis()

    def current(self, restaurant_id: object) -> int:
        redis_client = self.redis
        if redis_client is None:
            return 0
        try:
            value = redis_client.get(self._key(restaurant_id))
        except Exception:
            logger.warning("[AI cache] menu_generation_read_failed=true")
            return 0
        if value is None:
            return 0
        # O cliente compartilhado devolve BYTES (`decode_responses=False`, por
        # causa do vetor binario do embedding), e `int(b"7")` levanta
        # `TypeError` em vez de devolver 7. O `decode` cobre os dois casos:
        # bytes do cliente real, `str` de um cliente injetado em teste.
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def bump(self, restaurant_id: object) -> None:
        """Chamado pelo reindex depois de gravar. Falha aqui nao pode derrubar o worker.

        O custo de um incremento perdido e um cache de busca servindo o
        cardapio anterior ate o TTL — mesmo estado de quando nao ha Redis.
        Bem menor que o de abortar a indexacao por causa do cache.
        """
        redis_client = self.redis
        if redis_client is None:
            return
        try:
            redis_client.incr(self._key(restaurant_id))
        except Exception:
            logger.warning("[AI cache] menu_generation_bump_failed=true")

    @staticmethod
    def _key(restaurant_id: object) -> str:
        return f"ai:menu_generation:{restaurant_id}"


menu_generation = MenuGeneration()


class ChatCache:
    def __init__(
        self,
        embedding_ttl_seconds: int = _EMBEDDING_TTL_SECONDS,
        retrieval_ttl_seconds: int = _RETRIEVAL_TTL_SECONDS,
    ) -> None:
        self.embedding_ttl_seconds = embedding_ttl_seconds
        self.retrieval_ttl_seconds = retrieval_ttl_seconds
        self._embeddings: dict[str, _CacheEntry[list[float]]] = {}
        self._retrievals: dict[str, _CacheEntry[list[dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def normalize_message(message: str) -> str:
        normalized = unicodedata.normalize("NFKD", message.strip().lower())
        normalized = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
        return re.sub(r"\s+", " ", normalized)

    def embedding_key(self, restaurant_id: object, message: str) -> str:
        """Chave do vetor da PERGUNTA: `emb:v1:{modelo}:{restaurante}:{hash}`.

        De proposito sem a geracao do cardapio. O embedding de "tem pizza
        vegana?" e o mesmo antes e depois de o lojista mexer no menu — a
        pergunta nao mudou. Botar a geracao nesta chave jogaria fora, a cada
        reindex, justamente as chamadas que custam dinheiro.

        **E de proposito sem a FILIAL, pelo mesmo raciocinio.** O vetor e da
        frase, nao do cardapio: "tem picanha?" gera o mesmo vetor nas duas
        lojas. Botar a filial aqui compraria um embedding por loja para
        guardar N copias do mesmo numero. Quem precisa da filial e a chave da
        BUSCA, logo abaixo — e la ela e obrigatoria.

        =====================================================================
        OS TRES PEDACOS QUE ENTRARAM QUANDO A CHAVE FOI PARA O REDIS
        =====================================================================

        A chave era `{restaurant_id}:{mensagem normalizada}` e servia, porque
        vivia num `dict` que so aquele processo enxergava. No Redis ela e
        compartilhada entre workers, entre deploys e entre CLIENTES, e cada
        uma dessas coisas cobra um pedaco:

        **1. `emb:` — o prefixo.** O Redis e o mesmo do rate limit e do cache
        de entrega, e um namespace de chave sem prefixo e o jeito conhecido de
        duas coisas diferentes escreverem no mesmo lugar.

        **2. `v1:{modelo}` — a versao e o MODELO.** Este e o pedaco que
        impede um bug silencioso: `EMBEDDING_MODEL` sai do ambiente para poder
        mudar sem deploy, e o indice do `pgvector` e reconstruido pelo
        reindex. Sem o modelo na chave, no dia em que ele mudar o cache serve,
        por 60 minutos, vetor do modelo ANTIGO contra indice do NOVO. Nada
        falha: a busca devolve produtos, so que os errados. `v1` cobre o resto
        do formato — se a normalizacao, o digest ou o empacotamento mudarem, a
        versao sobe e as chaves velhas ficam inalcancaveis em vez de mal
        interpretadas.

        **3. `{hash}` — o digest, e nunca a mensagem crua.** O que o cliente
        digita nao vai para chave de Redis. E texto de pessoa, aparece em
        `KEYS`, em `MONITOR`, em qualquer dump — e o `ai_feedback` ja mostrou
        que gente escreve endereco e telefone para o Rapi
        (`feedback_retention_cutoff`). O digest tambem conserta um problema
        chato do outro lado: mensagem com espaco, dois-pontos ou quebra de
        linha deixa de poder empurrar o formato da chave.

        O GANHO NAO E O REUSO NA MESMA SESSAO — esse o `dict` ja dava. E o
        reuso ENTRE CLIENTES: "quanto custa a picanha" vinda de cinquenta
        pessoas diferentes, em workers diferentes, depois de um deploy, e uma
        chave so. Em memoria de processo isso nunca acontecia.
        """
        return (
            f"emb:{_EMBEDDING_KEY_VERSION}:{settings.EMBEDDING_MODEL}"
            f":{restaurant_id}:{self.message_digest(message)}"
        )

    @staticmethod
    def message_digest(message: str) -> str:
        """O hash da mensagem NORMALIZADA — a normalizacao vem antes, sempre.

        Invertida a ordem, "Quanto custa a PICANHA?" e "quanto custa a
        picanha?" dariam digests diferentes e pagariam dois embeddings, que e
        exatamente o acerto que `normalize_message` existe para produzir.

        32 caracteres hexadecimais sao 128 bits. Colisao aqui serviria o vetor
        de outra pergunta, entao o corte nao pode ser curto — mas 128 bits
        tambem nao e um numero que se alcance com o trafego de um cardapio.
        """
        normalized = ChatCache.normalize_message(message)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    def retrieval_key(
        self,
        restaurant_id: object,
        branch_id: object,
        message: str,
        max_price: object = None,
        top_k: object = None,
    ) -> str:
        """Chave dos produtos encontrados. Com a geracao, porque o resultado envelhece.

        O reindex incrementa a geracao daquele restaurante e todas as entradas
        anteriores deixam de ser alcancaveis — sem varrer, sem apagar, sem
        precisar saber quais perguntas estavam guardadas. As antigas caem
        sozinhas quando o TTL vence.

        **`branch_id` entra na chave desde a revisao 20260820_0026, e sem ele
        o filtro por filial da busca nao valeria nada:** a primeira loja a
        perguntar "tem picanha?" guardaria a resposta dela por 20 minutos, e a
        segunda receberia essa mesma lista — com os produtos e os ids da
        primeira. O Rapi ofereceria, com preco, um produto que aquela loja nao
        vende, e o cache faria o defeito parecer intermitente.

        `max_price` entra pelo mesmo tipo de motivo: muda o CONJUNTO
        devolvido. Sem ele, "sobremesa ate R$ 20" seria servida do cache de
        "sobremesa" sem teto, e o cliente veria produtos acima do que pediu.

        `top_k` ENTROU EM 25/08/2026, e ate esse dia a ausencia dele era
        inofensiva por acidente: ninguem passava `top_k`, entao toda consulta
        pedia cinco e todas as chaves descreviam o mesmo conjunto.

        A voz quebrou isso ao pedir uma busca larga para poder ordenar por
        preco. Sem `top_k` na chave, as duas consultas — a de cinco do `/chat`
        e a de quarenta da voz — dividem a mesma entrada, e quem chegar
        primeiro serve o outro pelos 20 minutos seguintes: ou a voz ordena
        cinco achando que sao quarenta, ou o `/chat` recebe quarenta produtos
        para escolher tres. Nenhum dos dois levanta erro.

        `None` continua escrevendo a chave curta de antes, entao o `/chat` —
        que nao passa `top_k` — nao muda de comportamento; a diferenca e que
        agora isso e escolha, e nao sorte.
        """
        generation = menu_generation.current(restaurant_id)
        teto = "" if max_price is None else f":p{max_price}"
        quantos = "" if top_k is None else f":k{top_k}"
        return (
            f"{restaurant_id}:{branch_id}:g{generation}{teto}{quantos}"
            f":{self.normalize_message(message)}"
        )

    def get_embedding(self, key: str) -> tuple[list[float] | None, str | None]:
        """O vetor e DE ONDE ele veio. Quatro origens, e o MISS tem tres delas.

        A origem faz parte do retorno porque ela e o unico jeito de saber se o
        item 1 desta frente funcionou. `embedding_cache_hit=true` nao
        distingue o acerto que o `dict` do processo ja dava do acerto NOVO —
        o de outro cliente, outro worker ou outro deploy —, e o segundo e o
        motivo inteiro de o cache ter ido para o Redis. Sem separar os dois no
        log, o ganho medido seria o antigo.

        Redis serve so de segundo nivel: acerto dele repovoa a memoria, para
        o proximo turno do mesmo processo nao pagar nem a ida a rede.

        =====================================================================
        POR QUE O MISS NAO PODE SER UMA ORIGEM SO
        =====================================================================

        Ate 24/08/2026 todo miss devolvia `None`, e o log do `/chat` escrevia
        `embedding_cache_origem=nenhuma` para TRES situacoes que pedem tres
        acoes diferentes:

        | origem          | o que aconteceu                  | o que fazer         |
        |-----------------|----------------------------------|---------------------|
        | `sem_redis`     | `REDIS_URL` ausente no ambiente  | conferir o `.env`   |
        | `redis_falhou`  | Redis recusou ou nao respondeu   | senha, host, rede   |
        | `nenhuma`       | Redis respondeu e nao tinha      | nada: cache frio    |

        As duas primeiras sao DEFEITO DE CONFIGURACAO e a terceira e o
        funcionamento normal de uma pergunta inedita. Sem separa-las, uma
        bateria de perguntas distintas contra um Redis que nunca esteve
        ligado produz exatamente o mesmo log de uma bateria contra um Redis
        saudavel — que foi o que custou a investigacao de 24/08/2026. O
        `.env` desta VPS ja perdeu configuracao duas vezes, e um cache que
        some sem dizer nada nao tem como ser notado: ele nao quebra resposta
        nenhuma, so volta a cobrar ~400 ms e uma chamada paga por pergunta.

        O aviso de boot correspondente esta em
        `collect_configuration_warnings` (`src/core/startup_checks.py`) — os
        dois cobrem instantes diferentes: o aviso pega o `.env` incompleto
        antes do primeiro cliente, a origem pega o Redis que caiu depois.
        """
        value = self._get(self._embeddings, key)
        if value is not None:
            return list(value), "memoria"

        value, origem = self._ler_embedding_do_redis(key)
        if value is None:
            return None, origem

        self._set(self._embeddings, key, value, self.embedding_ttl_seconds)
        return list(value), origem

    def set_embedding(self, key: str, embedding: list[float]) -> None:
        """Grava nos dois niveis. Falha no Redis nao impede a gravacao em memoria."""
        self._set(
            self._embeddings,
            key,
            list(embedding),
            self.embedding_ttl_seconds,
        )
        self._gravar_embedding_no_redis(key, embedding)

    def _ler_embedding_do_redis(
        self, key: str
    ) -> tuple[list[float] | None, str | None]:
        """Le, desempacota, e diz POR QUE nao trouxe nada quando nao trouxe.

        Um Redis fora do ar, um valor truncado ou um formato que mudou tem que
        significar "pague o embedding de novo" — que custa ~400 ms —, e nunca
        derrubar a resposta do cliente. O cache e desempenho; a pergunta dele,
        nao. Nenhum caminho daqui levanta.

        Devolve a origem junto porque quem chama nao tem como distinguir os
        casos sozinho: da altura do `get_embedding`, "nao havia Redis" e
        "havia e a chave nao estava la" sao os dois um `None`. A tabela das
        tres origens de miss esta no docstring dele.

        O acerto tambem sai por aqui (`"redis"`), para o `set` da memoria
        continuar sendo responsabilidade de um lugar so.
        """
        redis_client = cliente_redis()
        if redis_client is None:
            return None, "sem_redis"
        try:
            raw = redis_client.get(key)
        except Exception:
            logger.warning("[AI cache] embedding_redis_read_failed=true")
            return None, "redis_falhou"
        if not raw:
            return None, None

        # Valor truncado ou gravado noutro formato continua sendo um miss (ver
        # `_desempacotar`), mas NAO e cache frio: alguem gravou 6 KB de lixo
        # naquela chave, ou o empacotamento mudou sem a versao da chave subir.
        # Sai como `redis_falhou` para nao se esconder entre as perguntas
        # ineditas.
        vetor = self._desempacotar(raw)
        if vetor is None:
            logger.warning("[AI cache] embedding_redis_formato_invalido=true")
            return None, "redis_falhou"
        return vetor, "redis"

    def _gravar_embedding_no_redis(self, key: str, embedding: list[float]) -> None:
        """`SETEX`, e o TTL nao e opcional.

        O `docker-compose.yml` roda o Redis com `maxmemory-policy volatile-lru`,
        que so sabe descartar chave COM expiracao. Uma chave de embedding sem
        TTL seria 6 KB imortais por pergunta, num Redis de 256 MB que tambem
        guarda os contadores de rate limit — e sob pressao o descarte cairia
        inteiro sobre eles.
        """
        redis_client = cliente_redis()
        if redis_client is None:
            return
        try:
            redis_client.setex(
                key, self.embedding_ttl_seconds, self._empacotar(embedding)
            )
        except Exception:
            logger.warning("[AI cache] embedding_redis_write_failed=true")

    @staticmethod
    def _empacotar(embedding: list[float]) -> bytes:
        """O vetor em float32 binario, e a precisao perdida aqui e zero na pratica.

        JSON seria o obvio e custaria ~30 KB por vetor (1536 numeros escritos
        por extenso) contra 6 KB do binario — cinco vezes o espaco no mesmo
        Redis de 256 MB, e o tempo de serializar em toda leitura.

        FLOAT32 NAO PERDE NADA QUE O BANCO JA NAO PERCA. A coluna
        `ai_product_embeddings.embedding` e `vector` do pgvector, que armazena
        `float4` — 32 bits. O vetor da PERGUNTA e comparado contra vetores que
        ja passaram por essa reducao, e o proprio Postgres reduz o operando da
        consulta ao mesmo tipo. Guardar float64 aqui seria pagar o dobro para
        preservar bits que nunca chegam a influenciar a similaridade.

        `<` (little-endian explicito) e nao `=`: o formato nativo depende da
        maquina, e um valor gravado por um container e lido por outro nao pode
        depender disso.
        """
        return struct.pack(f"<{len(embedding)}f", *embedding)

    @staticmethod
    def _desempacotar(raw: bytes) -> list[float] | None:
        """Bytes de volta a lista. Tamanho que nao fecha em float32 vira `None`.

        Valor truncado ou gravado em outro formato tem que virar um miss, e
        nunca uma lista com o tamanho errado: um vetor de dimensao errada nao
        falha na busca, ele falha no `pgvector`, dentro da transacao do
        cliente.
        """
        if not raw or len(raw) % 4:
            return None
        return list(struct.unpack(f"<{len(raw) // 4}f", raw))

    def get_retrieval(self, key: str) -> list[dict[str, Any]] | None:
        return self._get(self._retrievals, key)

    def set_retrieval(self, key: str, products: list[dict[str, Any]]) -> None:
        self._set(
            self._retrievals,
            key,
            products,
            self.retrieval_ttl_seconds,
        )

    def _get(self, cache: dict[str, _CacheEntry[T]], key: str) -> T | None:
        now = monotonic()
        with self._lock:
            entry = cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                del cache[key]
                return None
            return deepcopy(entry.value)

    def _set(
        self,
        cache: dict[str, _CacheEntry[T]],
        key: str,
        value: T,
        ttl_seconds: int,
    ) -> None:
        now = monotonic()
        with self._lock:
            expired_keys = [
                cached_key
                for cached_key, entry in cache.items()
                if entry.expires_at <= now
            ]
            for expired_key in expired_keys:
                del cache[expired_key]
            cache[key] = _CacheEntry(
                value=deepcopy(value),
                expires_at=now + ttl_seconds,
            )


chat_cache = ChatCache()
