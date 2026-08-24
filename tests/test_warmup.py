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

import pytest

from src.core import warmup
from src.core.config import settings


class ExplodeAoAquecer:
    def __call__(self, *args, **kwargs):
        raise RuntimeError("a OpenAI esta fora do ar")


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
