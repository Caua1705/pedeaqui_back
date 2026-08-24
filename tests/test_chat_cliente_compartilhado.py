"""O `/chat` reusa um cliente HTTP por modelo, em vez de abrir um por pergunta.

O DEFEITO. `ChatLLMService` e construido por REQUISICAO, e construia um
`ChatOpenAI` novo junto — com cliente HTTP novo e pool de conexoes novo, o
que significa handshake TLS com a OpenAI em toda pergunta do cliente.

E o mesmo defeito que `embedding_service.py` ja tinha e ja consertou, com a
medicao registrada no docstring de la: mediana de 654 ms para 340 ms, 314 ms
de diferenca em 11 de 12 pares intercalados. O caminho do LLM ficou de fora
daquela rodada.

POR QUE ISTO PRECISA DE TESTE. O ganho e invisivel: nada falha quando o
cliente volta a ser criado por requisicao — so fica ~300 ms mais lento, sem
erro e sem log. Uma refatoracao que troque `get_chat_client(...)` de volta por
`ChatOpenAI(...)` passa em toda a suite e desfaz o conserto em silencio.

Nada aqui chama a OpenAI: construir o `ChatOpenAI` nao abre conexao.
"""

from src.ai.services.chat_llm_service import ChatLLMService, get_chat_client
from src.core.config import settings


class TestClienteCompartilhado:
    def test_duas_requisicoes_reusam_o_mesmo_cliente_http(self):
        """O que economiza os ~300 ms: o pool de conexoes sobrevive ao turno."""
        primeira, segunda = ChatLLMService(), ChatLLMService()

        assert primeira.llm is segunda.llm
        assert primeira.llm.root_client is segunda.llm.root_client

    def test_trocar_de_modelo_sem_deploy_continua_valendo(self, monkeypatch):
        """A chave do cache e o NOME DO MODELO, e este teste e o motivo.

        `MODEL_NAME` sai do ambiente para poder mudar sem deploy — trocar de
        modelo, ou fugir de uma indisponibilidade da OpenAI. Um cache sem
        chave congelaria o primeiro modelo visto no processo e a variavel
        voltaria a ser configuracao morta, que e exatamente o defeito que
        `test_chat_llm_model.py` registra.
        """
        monkeypatch.setattr(settings, "MODEL_NAME", "gpt-5-nano")
        nano = ChatLLMService()

        monkeypatch.setattr(settings, "MODEL_NAME", "gpt-5-mini")
        mini = ChatLLMService()

        assert nano.llm.model_name == "gpt-5-nano"
        assert mini.llm.model_name == "gpt-5-mini"
        assert nano.llm is not mini.llm

    def test_o_cache_e_por_modelo_e_nao_por_chamada(self):
        assert get_chat_client("gpt-5-mini") is get_chat_client("gpt-5-mini")
        assert get_chat_client("gpt-5-mini") is not get_chat_client("gpt-5-nano")

    def test_os_parametros_da_chamada_nao_mudaram(self):
        """O compartilhamento nao podia mexer em como o modelo responde.

        Sao os quatro que decidem o formato e o custo da resposta. Se algum
        mudar junto com o cache, o ganho de tempo vira mudanca de
        comportamento disfarcada de otimizacao.
        """
        llm = ChatLLMService().llm

        assert llm.reasoning_effort == "minimal"
        # `max_completion_tokens` e apelido de `max_tokens` no langchain-openai:
        # o atributo que sobra no objeto e o segundo nome.
        assert llm.max_tokens == 300
        assert llm.use_responses_api is True
        assert llm.model_name == settings.MODEL_NAME
