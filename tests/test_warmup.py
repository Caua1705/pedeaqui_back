"""O aquecimento do boot nunca pode derrubar o boot.

O QUE ELE FAZ. Paga no lifespan o que a primeira requisicao do processo
pagava sozinha — medido em producao em 24/08/2026: `embedding_ms=3534` no
primeiro turno contra ~400 nos seguintes, e `restaurant_lookup_ms=78` contra
~12. Sao a abertura das conexoes com a OpenAI e com o Postgres.

A PROPRIEDADE QUE ESTE ARQUIVO PROTEGE E A OUTRA. `warm_up` roda ao lado de
`validate_settings`, que RECUSA subir quando falta configuracao — e as duas
linhas precisam continuar tendo comportamentos opostos. Configuracao faltando
produz uma API que aceita pedido e nao cumpre; aquecimento que falhou produz
uma API ~3 s mais lenta na primeira pergunta. Se um dia o `try` daqui sumir
numa refatoracao, a OpenAI fora do ar passa a derrubar cardapio, pedido e
pagamento junto — que nao dependem dela.
"""

import threading
import time

import pytest

from src.core import warmup
from src.core.config import settings


class ExplodeAoAquecer:
    def __call__(self, *args, **kwargs):
        raise RuntimeError("a OpenAI esta fora do ar")


class ClienteQueRegistraAChamada:
    """Finge o cliente compartilhado e anota que a chamada REAL aconteceu."""

    def __init__(self, chamadas: list, rotulo: str):
        self.chamadas = chamadas
        self.rotulo = rotulo

    def embed_query(self, _texto):
        self.chamadas.append(self.rotulo)

    def invoke(self, _prompt):
        self.chamadas.append(self.rotulo)


class TestNuncaDerrubaOBoot:
    def test_openai_fora_do_ar_nao_levanta(self, monkeypatch):
        monkeypatch.setattr(settings, "AI_WARMUP_ENABLED", True)
        monkeypatch.setattr(warmup, "get_embeddings_client", ExplodeAoAquecer())
        monkeypatch.setattr(warmup, "get_chat_client", ExplodeAoAquecer())
        monkeypatch.setattr(warmup, "SessionLocal", ExplodeAoAquecer())

        warmup.warm_up()

    def test_banco_fora_do_ar_nao_impede_o_resto(self, monkeypatch):
        """Cada etapa cai sozinha: o banco fora nao pode pular o embedding."""
        aquecidos = []
        monkeypatch.setattr(settings, "AI_WARMUP_ENABLED", True)
        monkeypatch.setattr(warmup, "SessionLocal", ExplodeAoAquecer())
        monkeypatch.setattr(
            warmup,
            "get_embeddings_client",
            lambda: type("C", (), {"embed_query": lambda _s, _t: aquecidos.append("embedding")})(),
        )
        monkeypatch.setattr(warmup, "get_chat_client", lambda _m: aquecidos.append("llm"))

        warmup.warm_up()

        assert aquecidos == ["embedding", "llm"]

    @pytest.mark.parametrize(
        "etapa", ["get_embeddings_client", "get_chat_client", "SessionLocal"]
    )
    def test_a_falha_sai_como_warning(self, monkeypatch, caplog, etapa):
        """Engolir nao e esconder: sem o log, o boot lento fica sem explicacao."""
        monkeypatch.setattr(settings, "AI_WARMUP_ENABLED", True)
        monkeypatch.setattr(warmup, "get_embeddings_client", lambda: None)
        monkeypatch.setattr(warmup, "get_chat_client", lambda _m: None)
        monkeypatch.setattr(warmup, etapa, ExplodeAoAquecer())

        with caplog.at_level("WARNING", logger="uvicorn.error"):
            warmup.warm_up()

        assert "nao aqueceu" in caplog.text


class TestAChamadaEDeVerdade:
    """O item B: construir o objeto NAO e aquecer.

    A primeira versao so chamava `get_chat_client(...)` e registrava "pronto
    em 2.36 ms" — o proprio numero denunciava que nada tinha ido a rede. O
    handshake continuou inteiro no primeiro turno de producao: 3616 ms para 85
    tokens contra 1744 ms para 67 no seguinte.
    """

    def test_o_llm_recebe_uma_geracao_e_nao_so_a_construcao(self, monkeypatch):
        chamadas: list = []
        monkeypatch.setattr(settings, "AI_WARMUP_ENABLED", True)
        monkeypatch.setattr(warmup, "SessionLocal", ExplodeAoAquecer())
        monkeypatch.setattr(warmup, "get_embeddings_client", ExplodeAoAquecer())
        monkeypatch.setattr(
            warmup,
            "get_chat_client",
            lambda _m: ClienteQueRegistraAChamada(chamadas, "llm.invoke"),
        )

        warmup.warm_up()

        assert chamadas == ["llm.invoke"]

    def test_o_embedding_recebe_um_embed_query(self, monkeypatch):
        chamadas: list = []
        monkeypatch.setattr(settings, "AI_WARMUP_ENABLED", True)
        monkeypatch.setattr(warmup, "SessionLocal", ExplodeAoAquecer())
        monkeypatch.setattr(warmup, "get_chat_client", ExplodeAoAquecer())
        monkeypatch.setattr(
            warmup,
            "get_embeddings_client",
            lambda: ClienteQueRegistraAChamada(chamadas, "embedding.embed_query"),
        )

        warmup.warm_up()

        assert chamadas == ["embedding.embed_query"]

    def test_a_chamada_vai_pelo_cliente_compartilhado(self, monkeypatch):
        """O pool aquecido tem que ser o que a requisicao seguinte reusa.

        Um `ChatOpenAI` proprio do warmup abriria a conexao dele, aqueceria o
        pool dele e nao serviria para nada.
        """
        modelos: list = []
        monkeypatch.setattr(settings, "AI_WARMUP_ENABLED", True)
        monkeypatch.setattr(settings, "MODEL_NAME", "gpt-5-mini")
        monkeypatch.setattr(warmup, "SessionLocal", ExplodeAoAquecer())
        monkeypatch.setattr(warmup, "get_embeddings_client", ExplodeAoAquecer())
        monkeypatch.setattr(
            warmup,
            "get_chat_client",
            lambda modelo: modelos.append(modelo) or ClienteQueRegistraAChamada([], "x"),
        )

        warmup.warm_up()

        assert modelos == ["gpt-5-mini"]


class TestTimeout:
    def test_o_boot_nao_espera_alem_do_teto(self, monkeypatch):
        """A propriedade que o item B pediu: warmup lento nao pendura o boot."""
        monkeypatch.setattr(settings, "AI_WARMUP_ENABLED", True)
        monkeypatch.setattr(settings, "AI_WARMUP_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setattr(warmup, "SessionLocal", ExplodeAoAquecer())
        monkeypatch.setattr(warmup, "get_chat_client", ExplodeAoAquecer())

        pendurado = threading.Event()

        def nunca_responde():
            pendurado.wait(30)

        monkeypatch.setattr(
            warmup,
            "get_embeddings_client",
            lambda: type("C", (), {"embed_query": lambda _s, _t: nunca_responde()})(),
        )

        started_at = time.perf_counter()
        warmup.warm_up()
        decorrido = time.perf_counter() - started_at
        pendurado.set()

        assert decorrido < 3

    def test_estourar_o_teto_nao_cancela_o_aquecimento(self, monkeypatch):
        """Desistir de ESPERAR e diferente de desistir de aquecer.

        A thread e daemon e continua: se a chamada estava so lenta, o pool
        fica quente do mesmo jeito e o cliente seguinte se beneficia. Abortar
        deixaria o pior dos dois mundos — boot atrasado E conexao fria.
        """
        monkeypatch.setattr(settings, "AI_WARMUP_ENABLED", True)
        monkeypatch.setattr(settings, "AI_WARMUP_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setattr(warmup, "SessionLocal", ExplodeAoAquecer())
        monkeypatch.setattr(warmup, "get_chat_client", ExplodeAoAquecer())

        terminou = threading.Event()

        def lento():
            time.sleep(0.4)
            terminou.set()

        monkeypatch.setattr(
            warmup,
            "get_embeddings_client",
            lambda: type("C", (), {"embed_query": lambda _s, _t: lento()})(),
        )

        warmup.warm_up()

        assert terminou.wait(3) is True

    def test_o_estouro_sai_como_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "AI_WARMUP_ENABLED", True)
        monkeypatch.setattr(settings, "AI_WARMUP_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setattr(warmup, "SessionLocal", ExplodeAoAquecer())
        monkeypatch.setattr(warmup, "get_chat_client", ExplodeAoAquecer())
        monkeypatch.setattr(
            warmup,
            "get_embeddings_client",
            lambda: type("C", (), {"embed_query": lambda _s, _t: time.sleep(0.4)})(),
        )

        with caplog.at_level("WARNING", logger="uvicorn.error"):
            warmup.warm_up()

        assert "o boot seguiu e o aquecimento continua" in caplog.text


class TestDesligado:
    def test_desligado_nao_toca_em_nada(self, monkeypatch):
        """`AI_WARMUP_ENABLED=false` e o que mantem a suite sem rede.

        `tests/conftest.py` injeta esse valor no nivel do modulo. Sem ele,
        TODO teste que monta o TestClient chamaria a OpenAI de verdade no
        setup — o warmup e o unico ponto do boot que fala com servico
        externo.
        """
        monkeypatch.setattr(settings, "AI_WARMUP_ENABLED", False)
        monkeypatch.setattr(warmup, "get_embeddings_client", ExplodeAoAquecer())
        monkeypatch.setattr(warmup, "get_chat_client", ExplodeAoAquecer())
        monkeypatch.setattr(warmup, "SessionLocal", ExplodeAoAquecer())

        warmup.warm_up()

    def test_a_suite_roda_com_o_warmup_desligado(self):
        """A rede do conftest, conferida de dentro da propria suite."""
        assert settings.AI_WARMUP_ENABLED is False
