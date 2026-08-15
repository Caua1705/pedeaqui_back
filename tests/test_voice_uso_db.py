"""O consumo reportado no `/ended` — e o encerramento sem consumo nenhum.

**Por que existe.** Os seis contadores sao opcionais, e o motivo de serem
opcionais e operacional: o aviso de fim vai com `keepalive` de uma aba que
esta fechando, e pode chegar sem numero, chegar duas vezes, ou nao chegar. Um
schema que exigisse os numeros transformaria "nao deu tempo de medir" em
"a sessao nao encerrou".

E o caso do meio — sessao que a varredura do servidor ja fechou por teto de
duracao — e o que mais interessa medir: e a sessao mais longa da base, logo a
mais cara. Ela chega aqui com `ended_at` preenchido, e o numero dela nao pode
cair por causa disso.

Nada aqui fala com a OpenAI: nenhuma das sessoes tem `call_id`, entao o
desligamento remoto (a unica chamada de rede do caminho) nao acontece.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import voice as rota_de_voz
from src.api.dependencies.database import get_db
from src.models.ai_voice_session_model import AIVoiceSession
from tests.fabricas_db import criar_restaurante


pytestmark = pytest.mark.db

USO = {
    "input_audio_tokens": 4210,
    "input_text_tokens": 180,
    "output_audio_tokens": 9130,
    "output_text_tokens": 64,
    "cached_tokens": 1024,
    "duration_seconds": 96,
}


def test_o_encerramento_grava_o_consumo_reportado(db):
    sessao = _sessao(db)

    resposta = _encerrar(db, sessao, {"motivo": "o cliente clicou em Parar", **USO})

    assert resposta.status_code == 200
    assert resposta.json() == {"encerrado": True}
    db.refresh(sessao)
    assert sessao.ended_at is not None
    assert _uso_gravado(sessao) == USO


def test_a_sessao_que_nao_reporta_consumo_encerra_do_mesmo_jeito(db):
    sessao = _sessao(db)

    resposta = _encerrar(db, sessao, {"motivo": "a aba saiu de vista"})

    assert resposta.status_code == 200
    assert resposta.json() == {"encerrado": True}
    db.refresh(sessao)
    assert sessao.ended_at is not None
    # NULO e "nao reportado", e nao zero: um zero aqui viraria uma sessao
    # gratuita no relatorio, e sessao gratuita nao existe.
    assert _uso_gravado(sessao) == dict.fromkeys(USO, None)


def test_a_sessao_ja_fechada_pelo_servidor_ainda_grava_o_consumo(db):
    """A varredura do teto fecha a linha antes de o navegador reportar.

    A resposta continua sendo `encerrado: false` — quem encerrou foi o
    servidor, e o contrato da rota nao muda —, mas o numero fica.
    """
    sessao = _sessao(db)
    sessao.ended_at = datetime.now(timezone.utc)
    sessao.ended_reason = "teto de duracao — desligada pelo servidor"
    db.flush()

    resposta = _encerrar(db, sessao, {"motivo": "teto de 300s atingido", **USO})

    assert resposta.status_code == 200
    assert resposta.json() == {"encerrado": False}
    db.refresh(sessao)
    assert sessao.ended_reason == "teto de duracao — desligada pelo servidor"
    assert _uso_gravado(sessao) == USO


def test_contador_maior_que_o_inteiro_do_banco_e_recusado_na_validacao(db):
    """Sem o teto no schema isto nao seria 422: seria 500 no INSERT.

    A rota nao tem autenticacao, e a coluna e INTEGER.
    """
    sessao = _sessao(db)

    resposta = _encerrar(
        db, sessao, {"motivo": "numero absurdo", "input_audio_tokens": 9_000_000_000}
    )

    assert resposta.status_code == 422
    db.refresh(sessao)
    assert sessao.ended_at is None


def _sessao(db) -> AIVoiceSession:
    """Uma sessao aberta, sem `call_id` — nada aqui liga para a OpenAI."""
    sessao = AIVoiceSession(
        restaurant_id=criar_restaurante(db).id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
    )
    db.add(sessao)
    db.flush()
    return sessao


def _encerrar(db, sessao: AIVoiceSession, corpo: dict):
    app = FastAPI()
    app.include_router(rota_de_voz.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app).post(f"/voice/session/{sessao.id}/ended", json=corpo)


def _uso_gravado(sessao: AIVoiceSession) -> dict:
    return {campo: getattr(sessao, campo) for campo in USO}
