"""O campo `voz` do `POST /voice/session` — o seletor de timbre da bancada.

Nao leva marcador `db`: sao um schema, uma funcao de decisao e o corpo que vai
para a OpenAI. Nada aqui toca no banco.

**Por que existe.** Este campo e a UNICA coisa da sessao que a pagina do
cliente escolhe. Todo o resto — modelo, instrucoes, ferramentas, VAD — e
fixado no servidor de proposito, e o cabecalho de `realtime_client.py` diz por
que. Uma excecao dessas so pode existir com as duas travas no lugar, e o que
estes testes travam e exatamente elas:

    lista fechada     nome fora de `VOZES_DO_REALTIME` morre em 422, e nao em
                      502 depois de a rota ja ter gasto cota e uma emissao
    chave desligada   com `VOICE_ALLOW_VOICE_OVERRIDE=false`, o campo e po

Sem a primeira, a validacao viraria custo. Sem a segunda, o parametro de
bancada continuaria aceito na VPS depois de a voz ter sido escolhida.
"""

import uuid

import pytest
from pydantic import ValidationError

from src.ai.voice.realtime_client import VOZES_DO_REALTIME
from src.api.voice import SessaoRequest, _voz_da_sessao
from src.core.config import settings


def _corpo(**extra) -> dict:
    return {
        "restaurant_id": str(uuid.uuid4()),
        "branch_id": str(uuid.uuid4()),
        **extra,
    }


def test_producao_nao_manda_o_campo_e_isso_e_o_caminho_normal():
    """`None` nao e ausencia de escolha a ser preenchida depois: e a ordem de
    usar o `VOICE_NAME` do `.env`. O front do app nunca manda este campo."""
    assert SessaoRequest(**_corpo()).voz is None


@pytest.mark.parametrize("voz", VOZES_DO_REALTIME)
def test_toda_voz_da_lista_passa(voz: str):
    assert SessaoRequest(**_corpo(voz=voz)).voz == voz


@pytest.mark.parametrize("lixo", ["marim", "MARIN", "", "nova-voz-de-2027", "../etc"])
def test_o_que_nao_esta_na_lista_morre_no_schema(lixo: str):
    """422, e nao 502. O nome errado tem que morrer ANTES de a rota varrer
    vencidas, contar duas cotas e pagar uma emissao a OpenAI — que e onde ele
    morreria se a lista nao existisse."""
    with pytest.raises(ValidationError):
        SessaoRequest(**_corpo(voz=lixo))


def test_com_a_chave_desligada_a_escolha_e_ignorada_e_nao_vira_erro(monkeypatch):
    """O campo e de teste. Derrubar a emissao de quem mandou um campo a mais
    seria transformar experimento em incidente — e quem precisa saber o que
    valeu le o `voz` da resposta, nao um 4xx."""
    monkeypatch.setattr(settings, "VOICE_ALLOW_VOICE_OVERRIDE", False)

    assert _voz_da_sessao("sage") is None


def test_com_a_chave_ligada_a_escolha_vale(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_ALLOW_VOICE_OVERRIDE", True)

    assert _voz_da_sessao("sage") == "sage"


def test_a_chave_ligada_nao_inventa_voz_para_quem_nao_pediu(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_ALLOW_VOICE_OVERRIDE", True)

    assert _voz_da_sessao(None) is None


def test_a_voz_escolhida_chega_ao_corpo_que_vai_para_a_openai(monkeypatch):
    """O teste que fecha o circuito. Os de cima provam que a decisao esta
    certa; este prova que ela chega no `audio.output.voice` — sem ele, o
    parametro poderia estar sendo decidido com esmero e descartado depois.
    """
    from src.ai.voice import realtime_client

    enviados = {}

    class _Resposta:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"value": "ek_falso", "expires_at": 0}

    def _post_falso(url, headers=None, json=None, timeout=None):
        enviados.update(json)
        return _Resposta()

    monkeypatch.setattr(realtime_client.httpx, "post", _post_falso)

    realtime_client.issue_client_secret(uuid.uuid4(), "ctx", "loja", voz="cedar")
    assert enviados["session"]["audio"]["output"]["voice"] == "cedar"

    enviados.clear()
    realtime_client.issue_client_secret(uuid.uuid4(), "ctx", "loja")
    assert enviados["session"]["audio"]["output"]["voice"] == settings.VOICE_NAME
