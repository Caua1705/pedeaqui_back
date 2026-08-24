"""A rede embaixo de uma resposta que bateu no teto de tokens.

O INCIDENTE (24/08/2026, build `1ca8708`). O `/chat` caiu em producao com:

    pydantic_core.ValidationError: 1 validation error for ChatLLMResponse
      Invalid JSON: EOF while parsing a string at line 1 column 629

A resposta bateu no `max_completion_tokens` no meio da lista de
`selected_product_ids`. O modelo tinha escrito o texto INTEIRO — so os ids
ficaram pela metade — e nos jogavamos tudo fora e devolviamos erro ao cliente.

Perder os cartoes e um arranhao. Perder o turno e o cliente indo embora.

O DETALHE QUE DECIDE ONDE O CONSERTO MORA, e que so apareceu medindo contra a
API de verdade: **`parsing_error` nunca e preenchido neste modo de falha.** A
`ValidationError` sobe de dentro do SDK da OpenAI (`parse_response`), antes de
o `include_raw=True` do langchain ter o que embrulhar. Um `if` no retorno do
`invoke` — que era o desenho obvio — nunca teria rodado. Por isso o resgate e
um `except` em volta da chamada, e por isso estes testes constroem a excecao
em vez de um dicionario de resultado.

O JSON parcial vem de `ValidationError.errors()[0]["input"]`, que e a unica
copia dele que sobrevive: a resposta crua morre junto com a excecao.
"""

import json
import uuid

import pytest
from pydantic import ValidationError

from src.ai.schemas.chat_response_schema import ChatLLMResponse
from src.ai.services.chat_llm_service import ChatLLMService


ID_A = "a1b2c3d4-0000-4000-8000-000000000082"
ID_B = "f040b600-fb41-4cfd-9a11-2b7c8e5d0031"


def erro_de_json_cortado(parcial: str) -> ValidationError:
    """A excecao exatamente como o SDK da OpenAI a levanta: uma
    `ValidationError` de `ChatLLMResponse` sobre uma string de JSON invalida."""
    try:
        ChatLLMResponse.model_validate_json(parcial)
    except ValidationError as erro:
        return erro
    raise AssertionError("esse JSON era valido; o teste nao mede nada")


@pytest.fixture
def servico():
    return ChatLLMService()


class TestResgateDaRespostaCortada:
    def test_o_texto_sobrevive_ao_corte_no_meio_dos_ids(self, servico):
        """O caso do incidente, na forma em que ele aconteceu."""
        parcial = (
            '{"message":"Temos duas picanhas hoje: a **Importada** por '
            'R$ 189,90 e a **Black Angus** por R$ 229,90.",'
            '"response_type":"products","selected_product_ids":["' + ID_A + '","f040b600-fb41-4cfd'
        )

        resgatada = servico._resgatar_resposta_cortada(erro_de_json_cortado(parcial))

        assert resgatada is not None
        assert "Black Angus" in resgatada.message
        assert resgatada.message.endswith("R$ 229,90.")

    def test_os_ids_bem_formados_sao_preservados_e_o_cortado_nao(self, servico):
        """Um uuid pela metade nao vira `UUID`, e `ChatLLMResponse` recusaria o
        objeto inteiro — o resgate falharia tentando resgatar. Os completos sao
        legitimos: o modelo os escolheu, e do lado de fora eles ainda passam
        por `_validate_selected_product_ids`."""
        parcial = (
            '{"message":"Temos duas.","response_type":"products",'
            '"selected_product_ids":["' + ID_A + '","' + ID_B + '","c7e91a20-55d3'
        )

        resgatada = servico._resgatar_resposta_cortada(erro_de_json_cortado(parcial))

        assert resgatada.selected_product_ids == [uuid.UUID(ID_A), uuid.UUID(ID_B)]

    def test_o_response_type_sai_como_text_mesmo_havendo_ids(self, servico):
        """Quem decide isso e `ChatService._response_type`, olhando o que
        SOBROU da validacao. Dizer "products" aqui seria opinar sobre uma lista
        que ainda nao foi conferida."""
        parcial = (
            '{"message":"Temos duas.","response_type":"products",'
            '"selected_product_ids":["' + ID_A + '","c7e91a20-55d3'
        )

        resgatada = servico._resgatar_resposta_cortada(erro_de_json_cortado(parcial))

        assert resgatada.response_type == "text"

    def test_corte_antes_da_mensagem_nao_e_resgatavel(self, servico):
        """Sem `message` nao ha o que entregar, e devolver string vazia daria
        ao cliente uma resposta em branco com cara de sucesso. O chamador
        levanta a excecao original, que e o comportamento de antes."""
        resgatada = servico._resgatar_resposta_cortada(
            erro_de_json_cortado('{"messa')
        )

        assert resgatada is None

    def test_mensagem_so_de_espacos_conta_como_ausente(self, servico):
        resgatada = servico._resgatar_resposta_cortada(
            erro_de_json_cortado('{"message":"   ","response_type":"pro')
        )

        assert resgatada is None

    def test_erro_que_nao_e_de_json_invalido_passa_direto(self, servico):
        """O resgate cobre UM modo de falha: JSON cortado. Schema errado com
        JSON valido (campo faltando, tipo trocado) e outra coisa, e engoli-lo
        aqui esconderia um defeito nosso atras de uma resposta degradada."""
        try:
            ChatLLMResponse.model_validate_json(json.dumps({"message": "oi"}))
        except ValidationError as erro:
            assert servico._resgatar_resposta_cortada(erro) is None
        else:
            raise AssertionError("faltava response_type; isso tinha que falhar")

    def test_o_resgate_grita_no_log(self, servico, caplog):
        """Degradar em silencio faria o teto errado durar meses: o turno
        chegaria ao cliente mais pobre, sem cartao, e nada diria por que."""
        parcial = (
            '{"message":"Temos duas.","response_type":"products",'
            '"selected_product_ids":["' + ID_A + '","c7e91a20-55d3'
        )

        with caplog.at_level("WARNING", logger="uvicorn.error"):
            servico._resgatar_resposta_cortada(erro_de_json_cortado(parcial))

        assert "RESPOSTA CORTADA E RESGATADA" in caplog.text
        assert "AI_MAX_COMPLETION_TOKENS" in caplog.text


class TestAvisoDeTeto:
    """A linha que faltava no dia do incidente.

    Bater no teto nao tinha sintoma proprio: aparecia como `ValidationError` de
    JSON invalido, que parece defeito do modelo ou do schema. A primeira
    hipotese levantada na investigacao foi a errada por causa disso.
    """

    class RespostaCom:
        def __init__(self, saida):
            self.usage_metadata = {"output_tokens": saida}

    def test_resposta_encostada_no_teto_vira_aviso(self, servico, caplog, monkeypatch):
        from src.core.config import settings

        monkeypatch.setattr(settings, "AI_MAX_COMPLETION_TOKENS", 800)

        with caplog.at_level("WARNING", logger="uvicorn.error"):
            servico._avisar_se_bateu_no_teto(self.RespostaCom(790))

        assert "RESPOSTA NO LIMITE DO TETO" in caplog.text

    def test_resposta_folgada_nao_polui_o_log(self, servico, caplog, monkeypatch):
        from src.core.config import settings

        monkeypatch.setattr(settings, "AI_MAX_COMPLETION_TOKENS", 800)

        with caplog.at_level("WARNING", logger="uvicorn.error"):
            servico._avisar_se_bateu_no_teto(self.RespostaCom(180))

        assert "RESPOSTA NO LIMITE DO TETO" not in caplog.text

    def test_usage_ausente_nao_derruba_o_turno(self, servico):
        """Observabilidade nao pode custar a resposta que o cliente ja tem."""
        servico._avisar_se_bateu_no_teto(None)
        servico._avisar_se_bateu_no_teto(object())
