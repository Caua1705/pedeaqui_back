"""O leitor de log da bateria do prompt (`scripts/afere_prompt.py`).

POR QUE ISTO TEM TESTE. O script le os numeros do LOG, e nao do retorno de
`chat()` — o usage e o cronometro sao observabilidade e moram no logger. A
vantagem e que ele mede exatamente o que producao mede, com o mesmo formato;
o preco e que o acoplamento e a uma STRING DE FORMATO, e nao a uma interface.

Mude `_log_usage` ou `_log_prompt_size` e o coletor para de achar o que
procura. Sem este teste o sintoma seria uma tabela de comparacao com tracos
no lugar dos numeros, no fim de uma rodada de medicao que ja gastou dezoito
chamadas a OpenAI — e a conclusao sobre `reasoning_tokens`, que e o motivo da
bateria existir, simplesmente nao sairia.

As linhas abaixo sao copiadas das chamadas de `logger.info` de verdade. Se
alguem mudar o formato la, este arquivo e a lista do que precisa mudar junto.
"""

import logging
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ / "scripts") not in sys.path:
    sys.path.insert(0, str(RAIZ / "scripts"))

from afere_prompt import ColetorDeMedidas, _texto_tokens  # noqa: E402


@pytest.fixture
def coletor():
    log = logging.getLogger("uvicorn.error")
    handler = ColetorDeMedidas()
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    yield handler
    log.removeHandler(handler)


def _emite_turno_completo(log):
    """Um turno inteiro, nas linhas exatas que `ChatService` e
    `ChatLLMService` emitem hoje."""
    log.info("[AI /chat perf] cold_start=%s", "false")
    log.info(
        "[AI /chat prompt] system_chars=%d | context_chars=%d "
        "| branch_state_chars=%d "
        "| conversation_chars=%d | conversation_messages=%d "
        "| products_chars=%d | products_count=%d | user_chars=%d",
        3902, 31, 44, 210, 4, 880, 3, 23,
    )
    log.info("[AI /chat perf] retrieval_total_ms=%.2f", 22.10)
    log.info("[AI /chat perf] branch_state_ms=%.2f", 18.44)
    log.info("[AI /chat perf] llm_call_ms=%.2f", 2415.33)
    log.info(
        "[AI /chat usage] model=%s | input_tokens=%s | cached_input_tokens=%s "
        "| output_tokens=%s | reasoning_tokens=%s | total_tokens=%s",
        "gpt-5-mini", 1521, 0, 98, 16, 1619,
    )
    log.info("[AI /chat perf] total_ms=%.2f", 2501.02)


class TestColetorDeMedidas:
    def test_o_usage_inteiro_e_lido(self, coletor):
        """Os quatro numeros que decidem se a rodada valeu."""
        _emite_turno_completo(logging.getLogger("uvicorn.error"))

        assert coletor.turno["input_tokens"] == 1521
        assert coletor.turno["output_tokens"] == 98
        assert coletor.turno["reasoning_tokens"] == 16
        assert coletor.turno["total_tokens"] == 1619

    def test_cached_input_tokens_nao_e_confundido_com_input_tokens(self, coletor):
        """`cached_input_tokens` CONTEM `input_tokens` como substring.

        Um regex desatento le o numero do cache no campo da entrada, e a
        rodada inteira reporta 0 token de prompt sem errar nada visivel.
        """
        logging.getLogger("uvicorn.error").info(
            "[AI /chat usage] model=%s | input_tokens=%s | cached_input_tokens=%s "
            "| output_tokens=%s | reasoning_tokens=%s | total_tokens=%s",
            "gpt-5-mini", 1521, 1024, 98, 16, 1619,
        )

        assert coletor.turno["input_tokens"] == 1521
        assert coletor.turno["cached_tokens"] == 1024

    def test_total_ms_nao_pega_o_total_tokens(self, coletor):
        """As duas linhas tem "total" e passam pelo mesmo coletor."""
        _emite_turno_completo(logging.getLogger("uvicorn.error"))

        assert coletor.turno["total_ms"] == pytest.approx(2501.02)
        assert coletor.turno["total_tokens"] == 1619

    def test_os_cronometros_sao_lidos(self, coletor):
        _emite_turno_completo(logging.getLogger("uvicorn.error"))

        assert coletor.turno["llm_call_ms"] == pytest.approx(2415.33)
        assert coletor.turno["branch_state_ms"] == pytest.approx(18.44)
        assert coletor.turno["retrieval_total_ms"] == pytest.approx(22.10)

    def test_o_tamanho_do_prompt_por_secao_e_lido(self, coletor):
        """`branch_state_chars` e o unico pedaco do prompt que muda sozinho —
        uma loja que fecha faz a linha crescer sem ninguem editar nada."""
        _emite_turno_completo(logging.getLogger("uvicorn.error"))

        assert coletor.turno["system_chars"] == 3902
        assert coletor.turno["branch_state_chars"] == 44
        assert coletor.turno["products_count"] == 3

    def test_cold_start_vira_booleano(self, coletor):
        _emite_turno_completo(logging.getLogger("uvicorn.error"))

        assert coletor.turno["cold_start"] is False

    def test_zerar_nao_deixa_o_turno_anterior_vazar(self, coletor):
        """Sem isto, o turno da saudacao herdaria o usage do aquecimento e
        deixaria de parecer o que e: um turno SEM modelo."""
        _emite_turno_completo(logging.getLogger("uvicorn.error"))
        coletor.zerar()

        assert coletor.turno == {}

    def test_uma_linha_de_log_qualquer_nao_derruba_o_coletor(self, coletor):
        """O logger da aplicacao inteira passa por aqui, inclusive linhas com
        `%s` que nao interpolam e excecoes com stack trace."""
        log = logging.getLogger("uvicorn.error")
        log.info("[AI /chat] Nova requisicao | git_sha=%s", "874a5b7")
        log.warning("[AI /chat usage] usage_log_falhou=true")
        log.info("qualquer coisa sem numero nenhum")

        assert "output_tokens" not in coletor.turno


class TestTextoTokens:
    """`output_tokens` INCLUI `reasoning_tokens`, e essa e a armadilha inteira.

    Um prompt com mais regras pode encurtar o texto e alongar o raciocinio. As
    duas se cancelam, e o relatorio sairia dizendo "a resposta ficou 25% menor"
    com o turno demorando igual.
    """

    def test_o_raciocinio_sai_da_conta_do_texto(self):
        assert _texto_tokens({"output_tokens": 98, "reasoning_tokens": 16}) == 82

    def test_sem_raciocinio_o_texto_e_a_saida_inteira(self):
        assert _texto_tokens({"output_tokens": 98, "reasoning_tokens": None}) == 98
        assert _texto_tokens({"output_tokens": 98}) == 98

    def test_turno_sem_modelo_nao_tem_texto(self):
        """A saudacao enlatada nao chama a OpenAI: nao ha usage, e devolver 0
        a poria na mediana como uma resposta de comprimento zero."""
        assert _texto_tokens({}) is None
