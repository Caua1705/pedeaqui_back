"""O vetor da pergunta passa a viver no Redis — e o ganho e ENTRE CLIENTES.

O QUE ISTO MUDA EM RELACAO AO CACHE QUE JA EXISTIA. O `dict` de
`tests/test_chat_cache_de_embedding.py` continua no lugar e continua sendo o
primeiro nivel; o que ele nunca deu foi o acerto que paga a conta:

- ele morre a cada deploy, e o Rapi e deployado com frequencia;
- ele e por WORKER, entao a mesma pergunta em dois workers custa dois
  embeddings;
- e, sobretudo, ele nunca viu duas PESSOAS diferentes. "quanto custa a
  picanha" vinda de cinquenta clientes e, no Redis, uma chave so.

Este arquivo trava as quatro coisas que, se sumirem, transformam esse ganho em
defeito silencioso — nenhuma delas quebra um teste do arquivo antigo:

1. **O modelo esta na chave.** `EMBEDDING_MODEL` muda sem deploy e o reindex
   reconstroi o indice. Sem o modelo na chave, o cache serve vetor do modelo
   velho contra indice do novo por 60 minutos: a busca devolve produtos, so
   que os errados, e nada falha.
2. **A mensagem crua NAO esta na chave.** E texto de cliente, e ele escreve
   endereco e telefone para o Rapi (ver `feedback_retention_cutoff`).
3. **Redis fora do ar nao derruba a resposta.** Cache e desempenho; a
   pergunta do cliente, nao.
4. **O que volta do Redis e o mesmo vetor que entrou**, dentro da precisao de
   `float4` — que e a do `pgvector`, onde a comparacao acontece.
"""

import struct
import uuid

import pytest

from src.ai.services import chat_cache as cache_module
from src.ai.services.chat_cache import ChatCache
from tests.test_chat_cache_de_embedding import PERGUNTA, servico


VETOR = [0.1, -0.25, 0.5, 0.0, 1.5]


class RedisFalso:
    """O minimo que o cache usa: `get`, `setex`, `incr`. Em bytes, como o real."""

    def __init__(self):
        self.dados: dict[str, bytes] = {}
        self.leituras = 0
        self.escritas = 0

    def get(self, key):
        self.leituras += 1
        return self.dados.get(key)

    def setex(self, key, _ttl, value):
        self.escritas += 1
        self.dados[key] = value

    def incr(self, key):
        atual = int(self.dados.get(key, b"0"))
        self.dados[key] = str(atual + 1).encode()


class RedisQuebrado:
    """Todo comando explode — o Redis fora do ar, o disco cheio, a rede caida."""

    def get(self, _key):
        raise ConnectionError("redis fora do ar")

    def setex(self, _key, _ttl, _value):
        raise ConnectionError("redis fora do ar")


@pytest.fixture(autouse=True)
def cache_limpo():
    """O `chat_cache` e um singleton de modulo: sem isto, um teste vaza no outro."""
    cache_module.chat_cache._embeddings.clear()
    cache_module.chat_cache._retrievals.clear()
    yield
    cache_module.chat_cache._embeddings.clear()
    cache_module.chat_cache._retrievals.clear()


@pytest.fixture
def redis_falso(monkeypatch):
    falso = RedisFalso()
    monkeypatch.setattr(cache_module, "cliente_redis", lambda: falso)
    return falso


@pytest.fixture
def sem_redis(monkeypatch):
    monkeypatch.setattr(cache_module, "cliente_redis", lambda: None)


class TestAChave:
    def test_a_chave_leva_prefixo_versao_e_modelo(self, monkeypatch):
        """`emb:v1:{modelo}:{restaurante}:{hash}` — os quatro campos, nesta ordem."""
        monkeypatch.setattr(cache_module.settings, "EMBEDDING_MODEL", "text-embedding-3-small")
        restaurante = uuid.uuid4()

        chave = ChatCache().embedding_key(restaurante, PERGUNTA)

        prefixo, versao, modelo, dono, digest = chave.split(":")
        assert prefixo == "emb"
        assert versao == "v1"
        assert modelo == "text-embedding-3-small"
        assert dono == str(restaurante)
        assert len(digest) == 32

    def test_trocar_de_modelo_troca_a_chave(self, monkeypatch):
        """A armadilha inteira deste item: vetor velho contra indice novo.

        `EMBEDDING_MODEL` sai do ambiente justamente para mudar sem deploy. Se
        a chave nao acompanhar, o cache serve por 60 minutos vetores de um
        modelo contra um indice reconstruido por outro — sem erro, sem log, e
        com resultado pior.
        """
        cache = ChatCache()
        restaurante = uuid.uuid4()

        monkeypatch.setattr(cache_module.settings, "EMBEDDING_MODEL", "text-embedding-3-small")
        antes = cache.embedding_key(restaurante, PERGUNTA)
        monkeypatch.setattr(cache_module.settings, "EMBEDDING_MODEL", "text-embedding-3-large")
        depois = cache.embedding_key(restaurante, PERGUNTA)

        assert antes != depois

    @pytest.mark.parametrize(
        "mensagem",
        [
            "moro na rua das flores 200 apto 31",
            "meu telefone e 85 99999-1234",
            "tem picanha?",
        ],
    )
    def test_a_mensagem_crua_nunca_aparece_na_chave(self, mensagem):
        """Texto de cliente nao vai para chave de Redis — ele aparece em `KEYS`.

        A contraprova esta em `test_a_mesma_pergunta_da_a_mesma_chave`: sumir
        com o texto nao pode significar sumir com a identidade da pergunta.
        """
        chave = ChatCache().embedding_key(uuid.uuid4(), mensagem)

        assert mensagem not in chave
        # Palavra de ate 2 letras nao serve de evidencia: "e" cai dentro de
        # "emb:" e de qualquer hexadecimal, e o teste passaria a falhar por
        # coincidencia em vez de por vazamento.
        for palavra in mensagem.split():
            if len(palavra) > 2:
                assert palavra not in chave

    def test_a_mesma_pergunta_da_a_mesma_chave(self):
        """O digest tem que ser estavel, senao o cache nunca acerta."""
        cache = ChatCache()
        restaurante = uuid.uuid4()

        assert (cache.embedding_key(restaurante, PERGUNTA)
                == cache.embedding_key(restaurante, PERGUNTA))

    def test_caixa_e_acento_continuam_caindo_na_mesma_chave(self):
        """A normalizacao roda ANTES do hash. Invertido, sao dois embeddings."""
        cache = ChatCache()
        restaurante = uuid.uuid4()

        assert (cache.embedding_key(restaurante, "Quanto custa a PICANHA?  ")
                == cache.embedding_key(restaurante, "quanto custa a picanha?"))

    def test_perguntas_diferentes_dao_chaves_diferentes(self):
        cache = ChatCache()
        restaurante = uuid.uuid4()

        assert (cache.embedding_key(restaurante, "tem picanha?")
                != cache.embedding_key(restaurante, "tem pizza?"))


class TestOReusoEntreClientes:
    def test_um_processo_novo_aproveita_o_vetor_do_anterior(self, redis_falso):
        """O ganho que so o Redis da: memoria zerada, embedding nao recalculado.

        `ChatCache()` novo com `_embeddings` vazio e o que existe depois de um
        deploy, ou no segundo worker. Antes deste item, isso era um miss
        garantido.
        """
        primeiro = ChatCache()
        chave = primeiro.embedding_key(uuid.uuid4(), PERGUNTA)
        primeiro.set_embedding(chave, VETOR)

        depois_do_deploy = ChatCache()
        vetor, origem = depois_do_deploy.get_embedding(chave)

        assert origem == "redis"
        assert vetor == pytest.approx(VETOR, abs=1e-6)

    def test_o_acerto_do_redis_repovoa_a_memoria(self, redis_falso):
        """Segundo turno do mesmo processo nao paga nem a ida a rede."""
        cache = ChatCache()
        chave = cache.embedding_key(uuid.uuid4(), PERGUNTA)
        ChatCache().set_embedding(chave, VETOR)

        _, primeira_origem = cache.get_embedding(chave)
        leituras_ate_aqui = redis_falso.leituras
        _, segunda_origem = cache.get_embedding(chave)

        assert primeira_origem == "redis"
        assert segunda_origem == "memoria"
        assert redis_falso.leituras == leituras_ate_aqui

    def test_a_memoria_responde_antes_do_redis(self, redis_falso):
        """Quem gravou nao volta a rede para ler o que ele mesmo tem."""
        cache = ChatCache()
        chave = cache.embedding_key(uuid.uuid4(), PERGUNTA)
        cache.set_embedding(chave, VETOR)
        redis_falso.leituras = 0

        _, origem = cache.get_embedding(chave)

        assert origem == "memoria"
        assert redis_falso.leituras == 0

    def test_pergunta_nunca_vista_nao_acerta_nada(self, redis_falso):
        cache = ChatCache()
        chave = cache.embedding_key(uuid.uuid4(), "pergunta inedita")

        assert cache.get_embedding(chave) == (None, None)


class TestOEmpacotamento:
    def test_o_vetor_volta_igual_dentro_da_precisao_do_pgvector(self, redis_falso):
        """`float4` e o que a coluna `vector` guarda — a perda aqui ja existe la."""
        cache = ChatCache()
        chave = cache.embedding_key(uuid.uuid4(), PERGUNTA)
        original = [0.123456789, -0.987654321, 0.5, 0.0, -1.0]

        cache.set_embedding(chave, original)
        vetor, _ = ChatCache().get_embedding(chave)

        assert vetor == pytest.approx(original, rel=1e-6)

    def test_o_binario_e_float32_e_nao_json(self, redis_falso):
        """4 bytes por dimensao. JSON custaria ~5x o espaco no mesmo Redis."""
        cache = ChatCache()
        chave = cache.embedding_key(uuid.uuid4(), PERGUNTA)

        cache.set_embedding(chave, VETOR)

        gravado = redis_falso.dados[chave]
        assert isinstance(gravado, bytes)
        assert len(gravado) == len(VETOR) * 4

    def test_valor_truncado_vira_miss_e_nao_vetor_de_dimensao_errada(self, redis_falso):
        """Um vetor curto nao falha na busca: falha dentro do `pgvector`.

        Melhor pagar o embedding de novo do que mandar 3 dimensoes contra um
        indice de 1536 e receber o erro na transacao do cliente.
        """
        cache = ChatCache()
        chave = cache.embedding_key(uuid.uuid4(), PERGUNTA)
        redis_falso.dados[chave] = struct.pack("<3f", 0.1, 0.2, 0.3)[:-1]

        assert cache.get_embedding(chave) == (None, None)

    def test_ler_lixo_da_chave_nao_levanta_no_caminho_do_cliente(self, redis_falso):
        """Byte arbitrario cujo tamanho FECHA em float32 nao vira excecao.

        E de proposito que este teste nao exige `None`: 16 bytes desempacotam
        em 4 floats validos, e nao ha como o cache saber que aqueles quatro
        numeros nao sao um vetor. Quem barra dimensao errada e o `pgvector`,
        na busca. O que se garante AQUI e que ler lixo nao explode no caminho
        da pergunta do cliente — a garantia de `None` vale para o tamanho que
        nao fecha, no teste acima.
        """
        cache = ChatCache()
        chave = cache.embedding_key(uuid.uuid4(), PERGUNTA)
        redis_falso.dados[chave] = b"nao sou um vetor"

        vetor, origem = cache.get_embedding(chave)

        assert origem == "redis"
        assert len(vetor) == 4


class TestQuandoORedisNaoEsta:
    def test_sem_redis_o_cache_continua_funcionando_em_memoria(self, sem_redis):
        """Degradar em silencio e o comportamento certo: volta a ser o cache antigo."""
        cache = ChatCache()
        chave = cache.embedding_key(uuid.uuid4(), PERGUNTA)

        cache.set_embedding(chave, VETOR)
        vetor, origem = cache.get_embedding(chave)

        assert origem == "memoria"
        assert vetor == VETOR

    def test_redis_quebrado_nao_derruba_a_leitura(self, monkeypatch):
        monkeypatch.setattr(cache_module, "cliente_redis", lambda: RedisQuebrado())
        cache = ChatCache()

        assert cache.get_embedding("emb:v1:m:r:h") == (None, None)

    def test_redis_quebrado_nao_derruba_a_escrita_nem_perde_a_memoria(self, monkeypatch):
        """A gravacao em memoria acontece ANTES da do Redis, e sobrevive a ela."""
        monkeypatch.setattr(cache_module, "cliente_redis", lambda: RedisQuebrado())
        cache = ChatCache()

        cache.set_embedding("emb:v1:m:r:h", VETOR)

        assert cache.get_embedding("emb:v1:m:r:h") == (VETOR, "memoria")

    def test_o_chat_responde_com_o_redis_fora_do_ar(self, monkeypatch, caplog):
        """A ponta: uma pergunta de verdade atravessa o caminho inteiro."""
        monkeypatch.setattr(cache_module, "cliente_redis", lambda: RedisQuebrado())
        service = servico()

        with caplog.at_level("WARNING", logger="uvicorn.error"):
            produtos = service.retrieve_products(uuid.uuid4(), uuid.uuid4(), PERGUNTA)

        assert produtos
        assert service.embedding_service.chamadas == 1


class TestOLog:
    def test_a_origem_do_acerto_aparece_no_log(self, redis_falso, caplog):
        """Sem a origem, o acerto do Redis e indistinguivel do acerto do `dict`."""
        service = servico()
        restaurante, filial = uuid.uuid4(), uuid.uuid4()

        with caplog.at_level("INFO", logger="uvicorn.error"):
            service.retrieve_products(restaurante, filial, PERGUNTA)
            assert "embedding_cache_origem=nenhuma" in caplog.text
            caplog.clear()

            service.retrieve_products(restaurante, filial, PERGUNTA)

        assert "embedding_cache_origem=memoria" in caplog.text

    def test_processo_novo_registra_origem_redis(self, redis_falso, caplog):
        """O log que prova o item 1: acerto vindo de fora deste processo."""
        primeiro = servico()
        restaurante, filial = uuid.uuid4(), uuid.uuid4()
        primeiro.retrieve_products(restaurante, filial, PERGUNTA)

        cache_module.chat_cache._embeddings.clear()
        cache_module.chat_cache._retrievals.clear()
        segundo = servico()

        with caplog.at_level("INFO", logger="uvicorn.error"):
            segundo.retrieve_products(restaurante, filial, PERGUNTA)

        assert "embedding_cache_hit=true" in caplog.text
        assert "embedding_cache_origem=redis" in caplog.text
        assert segundo.embedding_service.chamadas == 0


class TestOContadorDeGeracao:
    def test_o_contador_le_bytes_do_cliente_compartilhado(self, redis_falso):
        """`decode_responses=False` fez `int(b"7")` virar `TypeError`.

        O cliente passou a ser um so no processo, e ele devolve bytes por
        causa do vetor binario. Sem o `decode` em `current`, toda leitura da
        geracao cairia no `except` e voltaria 0 — e o cache de busca deixaria
        de ser invalidado pelo reindex, em silencio.
        """
        geracao = cache_module.MenuGeneration()
        restaurante = uuid.uuid4()

        geracao.bump(restaurante)
        geracao.bump(restaurante)

        assert geracao.current(restaurante) == 2

    def test_sem_redis_a_geracao_fica_em_zero(self, sem_redis):
        assert cache_module.MenuGeneration().current(uuid.uuid4()) == 0
