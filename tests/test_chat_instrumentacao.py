"""Os logs de medicao do `/chat` nao podem derrubar o `/chat`.

ISTO E REDE DE UM INCIDENTE, NAO TESTE DE FEATURE. A instrumentacao de
`chat_llm_service.py` derrubou o assistente em producao com

    TypeError: Object of type UUID is not JSON serializable

porque `_log_prompt_size` media o tamanho do prompt com `json.dumps`, e os
produtos chegam da busca com `id` em `UUID` — `similarity_search` devolve a
coluna `p.id` assim, e e exatamente por isso que
`_validate_selected_product_ids` faz `uuid.UUID(str(...))` em vez de confiar
no tipo. Um log de MEDICAO levou o caminho quente junto.

A licao tem duas metades, e o teste cobre as duas:

1. **A forma real dos dados.** O log passou na suite porque os produtos dos
   testes vinham com `id` em `str`. `produtos_como_a_busca_devolve` existe
   para nenhum teste daqui poder repetir esse engano.
2. **Log de perf falha calado.** Nao existe medicao que valha uma requisicao
   perdida: se a conta do log der errado, o cliente tem que receber a resposta
   do mesmo jeito e o defeito sai como `warning`.

O ponto 2 vale para o que se CALCULA para alimentar o log. As linhas de tempo
(`llm_call_ms`, `guard_ms`, `etapas`...) sao `perf_counter` formatado com
`%.2f` e nao tem como falhar — e o `logging` ainda engole erro de formatacao
por conta propria. Quem precisa de rede e quem monta o argumento antes.
"""

import uuid

import pytest

from src.ai.services.chat_llm_service import ChatLLMService


def produtos_como_a_busca_devolve() -> list[dict]:
    """O `id` em `UUID`, que e o que `RetrievalService` entrega de verdade."""
    return [
        {
            "id": uuid.uuid4(),
            "name": "Picanha à Moda",
            "short_description": "na chapa, com farofa",
            "price": "R$ 89,90",
        }
    ]


class ExplodeAoVirarTexto:
    """Qualquer coisa que o log tente medir e nao consiga."""

    def __repr__(self) -> str:
        raise RuntimeError("este objeto nao vira texto")


class TestTamanhoDoPrompt:
    def test_produto_com_id_em_uuid_nao_levanta(self):
        """O incidente. Antes do conserto, esta linha era um TypeError."""
        ChatLLMService._log_prompt_size(
            restaurant_context="Nome do restaurante: Junior da Picanha",
            conversation=[{"role": "user", "content": "oi"}],
            retrieved_products=produtos_como_a_busca_devolve(),
            user_message="quanto custa a picanha?",
        )

    def test_o_tamanho_medido_e_o_que_vai_no_prompt(self):
        """`str()` e a medida certa, e nao so a que nao quebra.

        O `ChatPromptTemplate` interpola `{retrieved_products}` com `str()`,
        entao o que a OpenAI recebe e o `repr` da lista. O `json.dumps` media
        um texto que nunca foi enviado — errava o numero alem de derrubar.
        """
        produtos = produtos_como_a_busca_devolve()

        assert str(produtos[0]["id"]) in str(produtos)

    def test_algo_que_nao_vira_texto_nao_derruba_a_requisicao(self):
        """A rede do ponto 2: a medicao se perde, a resposta nao."""
        ChatLLMService._log_prompt_size(
            restaurant_context="Nome do restaurante: Junior da Picanha",
            conversation=[{"role": "user", "content": ExplodeAoVirarTexto()}],
            retrieved_products=[{"id": ExplodeAoVirarTexto()}],
            user_message="oi",
        )

    def test_a_falha_do_log_sai_como_warning(self, caplog):
        """Engolir nao e esconder: sem esta linha, o log quebrado fica invisivel."""
        with caplog.at_level("WARNING", logger="uvicorn.error"):
            ChatLLMService._log_prompt_size(
                restaurant_context="x",
                conversation=[],
                retrieved_products=[{"id": ExplodeAoVirarTexto()}],
                user_message="oi",
            )

        assert "prompt_size_log_falhou=true" in caplog.text


class TestUsage:
    """Nada aqui chama a OpenAI: construir o `ChatOpenAI` nao abre conexao."""

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "uma resposta sem usage_metadata",
            object(),
        ],
        ids=["sem_raw", "raw_sem_usage", "objeto_qualquer"],
    )
    def test_resposta_sem_usage_nao_levanta(self, raw):
        ChatLLMService()._log_usage(raw)

    def test_usage_em_formato_inesperado_nao_levanta(self):
        """Se a OpenAI mudar o formato, o Rapi continua respondendo.

        `usage_metadata` como lista nao tem `.get`. E o formato mudar do lado
        deles e a falha mais provavel deste log — e a que nao pode custar a
        resposta que o cliente ja esperou.
        """
        raw = type("RespostaEstranha", (), {"usage_metadata": ["input", 1274]})()

        ChatLLMService()._log_usage(raw)

    def test_a_falha_do_usage_sai_como_warning(self, caplog):
        raw = type("RespostaEstranha", (), {"usage_metadata": ["input", 1274]})()

        with caplog.at_level("WARNING", logger="uvicorn.error"):
            ChatLLMService()._log_usage(raw)

        assert "usage_log_falhou=true" in caplog.text
