"""O modelo do chat de texto sai de `settings.MODEL_NAME`.

`MODEL_NAME` existia no `.env.example` e no `config.py` e **nao era lida por
ninguem**: o `ChatLLMService` fixava `"gpt-5-mini"` no codigo. Configuracao
morta e pior que configuracao ausente — quem editasse a variavel para trocar
de modelo (testar um novo, fugir de indisponibilidade da OpenAI) veria o
sistema seguir no antigo, sem erro em lugar nenhum para denunciar.

Nada aqui chama a OpenAI: construir o `ChatOpenAI` nao abre conexao.
"""

from src.ai.services.chat_llm_service import ChatLLMService
from src.core.config import settings


def test_o_modelo_vem_da_configuracao(monkeypatch):
    monkeypatch.setattr(settings, "MODEL_NAME", "gpt-5-nano")

    assert ChatLLMService().llm.model_name == "gpt-5-nano"


def test_o_default_continua_sendo_o_valor_que_estava_fixo_no_codigo():
    """A ligacao nao podia mudar o comportamento de hoje: quem nao define
    `MODEL_NAME` no ambiente tem que continuar no mesmo modelo."""
    assert type(settings).model_fields["MODEL_NAME"].default == "gpt-5-mini"
