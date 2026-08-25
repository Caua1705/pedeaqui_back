"""O cursor de categorias, que mora na sessao. Precisa de banco: e uma coluna.

**Por que ele existe.** Em 25/08/2026, "voces tem mais o que?" — perguntado
depois de o atendente ja ter falado de duas categorias — recebeu a MESMA lista
e produziu as MESMAS duas. `listar_categorias` nao tem parametro e nao tem
memoria: duas chamadas iguais devolvem a mesma coisa, o que e correto para "o
que voces tem?" e e o defeito para "e o que mais?".

**Por que no banco, e nao num dicionario do processo.** Armadilha 20: nada em
memoria sobrevive a mais de um worker. A segunda chamada da mesma conversa cai
em outro processo, o cursor volta a zero, e o sintoma vira "as vezes ele
repete" — que e pior de achar do que "ele sempre repete".

**O que a coluna conta.** Quantas a FERRAMENTA entregou, nunca quantas o
assistente falou. O audio nao passa por aqui (armadilha 43), entao a segunda
coisa o backend nao tem como saber.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.ai.voice.session_service import VoiceSessionService
from src.api import voice as rota_de_voz
from src.api.dependencies.database import get_db
from src.models.ai_voice_session_model import AIVoiceSession
from tests.fabricas_db import (
    criar_categoria,
    criar_filial,
    criar_produto,
    criar_restaurante,
)


pytestmark = pytest.mark.db

PASSO = 2


def test_a_sessao_nova_comeca_do_inicio_da_lista(db):
    sessao = _sessao(db)

    assert _avancar(db, sessao.id, quantas_ha=4) == 0


def test_a_segunda_chamada_da_mesma_sessao_comeca_depois_das_ja_entregues(db):
    """O conserto inteiro: a mesma pergunta, na mesma conversa, nao devolve a
    mesma fatia."""
    sessao = _sessao(db)

    primeira = _avancar(db, sessao.id, quantas_ha=4)
    segunda = _avancar(db, sessao.id, quantas_ha=4)

    assert (primeira, segunda) == (0, 2)


def test_o_cursor_da_a_volta_no_fim_da_lista(db):
    """Quem pergunta quatro vezes numa loja de quatro categorias ouve a
    primeira de novo. Frase vazia seria pior — e nao ha "ja falei de todas" a
    dizer, porque a lista inteira vai na linha de DADOS de toda chamada."""
    sessao = _sessao(db)

    voltas = [_avancar(db, sessao.id, quantas_ha=4) for _ in range(3)]

    assert voltas == [0, 2, 0]


def test_uma_sessao_nao_move_o_cursor_da_outra(db):
    """O cursor e da CONVERSA. Fosse por restaurante, dois clientes falando ao
    mesmo tempo com a mesma loja se roubariam categoria."""
    uma = _sessao(db)
    outra = _sessao(db)

    _avancar(db, uma.id, quantas_ha=4)

    assert _avancar(db, outra.id, quantas_ha=4) == 0


def test_a_lista_que_encolheu_no_meio_da_conversa_nao_trava_o_cursor(db):
    """Produto esgotado esvazia uma categoria, e a lista da segunda chamada
    pode ser menor que o cursor da primeira. Sem o resto, a fatia sairia vazia
    para sempre — e o atendente ficaria sem frase no meio da conversa."""
    sessao = _sessao(db)
    _avancar(db, sessao.id, quantas_ha=8)
    _avancar(db, sessao.id, quantas_ha=8)

    assert _avancar(db, sessao.id, quantas_ha=3) == 1


def test_sem_sessao_o_cursor_nao_existe_e_a_ferramenta_responde_igual(db):
    """Front que nao mande `sessao_id` continua funcionando, sempre com as
    maiores. E o que impede um campo novo de virar 422 no meio de uma conversa
    falada."""
    assert _avancar(db, None, quantas_ha=4) == 0


def test_sessao_desconhecida_nao_levanta_no_meio_de_uma_ferramenta(db):
    """Id que nao existe mais — sessao ja limpa, aba velha — cai no mesmo
    caminho do sem-sessao. Uma ferramenta que estoura deixa o modelo esperando
    calado, que e o pior desfecho possivel para um erro deste tamanho."""
    from uuid import uuid4

    assert _avancar(db, uuid4(), quantas_ha=4) == 0


def test_loja_sem_categoria_nenhuma_nao_grava_cursor(db):
    """Dividir por zero e o unico jeito de esta funcao quebrar, e loja com o
    cardapio inteiro esgotado e um estado que acontece."""
    sessao = _sessao(db)

    assert _avancar(db, sessao.id, quantas_ha=0) == 0
    db.refresh(sessao)
    assert sessao.categories_offset == 0


# --------------------------------------------------------------------------
# E A ROTA, DE PONTA A PONTA
#
# O cursor certo com o `sessao_id` morrendo no meio do caminho e o formato de
# defeito que este projeto ja conhece: a funcao passa nos testes, a ferramenta
# responde 200, e o atendente repete as mesmas duas categorias. So a rota
# inteira prova que o campo atravessa.
# --------------------------------------------------------------------------


def test_a_segunda_pergunta_na_rota_nao_repete_as_categorias(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    for nome in ("Bebidas", "Carnes", "Massas", "Sobremesas"):
        categoria = criar_categoria(db, restaurante, nome=nome, filial=filial)
        criar_produto(db, restaurante, categoria, nome=f"Item de {nome}")
    sessao = _sessao(db, restaurante)
    db.flush()

    corpo = {
        "restaurant_id": str(restaurante.id),
        "branch_id": str(filial.id),
        "sessao_id": str(sessao.id),
    }
    cliente = _cliente(db)
    primeira = cliente.post("/voice/categories", json=corpo).json()["resumo"]
    segunda = cliente.post("/voice/categories", json=corpo).json()["resumo"]

    assert _frase(primeira) == "FRASE: Tem Bebidas e Carnes, e mais uns tipos."
    assert _frase(segunda) == "FRASE: Tem Massas e Sobremesas, e mais uns tipos."
    # E a lista inteira continua nas duas: ela nao serve so para falar, e o que
    # o modelo le antes de dizer que a loja nao tem alguma coisa.
    for nome in ("Bebidas", "Carnes", "Massas", "Sobremesas"):
        assert nome in primeira and nome in segunda


def test_sem_sessao_id_a_rota_responde_como_antes(db):
    """Front que ainda nao repassa o campo continua funcionando — sempre as
    maiores. E o que impede um campo novo de virar 422 no meio de uma conversa
    falada."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    for nome in ("Bebidas", "Carnes"):
        categoria = criar_categoria(db, restaurante, nome=nome, filial=filial)
        criar_produto(db, restaurante, categoria, nome=f"Item de {nome}")
    db.flush()

    corpo = {"restaurant_id": str(restaurante.id), "branch_id": str(filial.id)}
    cliente = _cliente(db)
    primeira = cliente.post("/voice/categories", json=corpo)
    segunda = cliente.post("/voice/categories", json=corpo)

    assert primeira.status_code == 200
    assert _frase(primeira.json()["resumo"]) == "FRASE: Tem Bebidas e Carnes."
    assert segunda.json()["resumo"] == primeira.json()["resumo"]


def _frase(resumo: str) -> str:
    return resumo.splitlines()[0]


def _cliente(db) -> TestClient:
    """App minimo com o router da voz. `/voice/categories` nao exige token."""
    app = FastAPI()
    app.include_router(rota_de_voz.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _avancar(db, sessao_id, quantas_ha: int) -> int:
    return VoiceSessionService(db).avancar_cursor_de_categorias(
        sessao_id,
        quantas_ha,
        PASSO,
    )


def _sessao(db, restaurante=None) -> AIVoiceSession:
    """Uma sessao aberta, sem `call_id` — nada aqui liga para a OpenAI."""
    sessao = AIVoiceSession(
        restaurant_id=(restaurante or criar_restaurante(db)).id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
    )
    db.add(sessao)
    db.flush()
    return sessao
