"""UM teste de fumaça do atendimento por voz. Não é suíte, e não deve virar uma.

**Por que existe.** A voz consome três métodos PRIVADOS do `ChatService` —
`_get_active_restaurant`, `_build_restaurant_context` e `_hydrate_products` —
além do atributo `retrieval_service`. Nenhum deles tem contrato com ninguém, e
renomear qualquer um passa na suíte inteira: a voz só quebra em runtime,
quando alguém abre a página e o microfone já está ligado.

Este teste é o que transforma "quebra em silêncio" em "quebra no CI". Ele
percorre o caminho inteiro uma vez — emitir credencial, chamar a ferramenta,
receber produto — e nada mais. Cobertura de borda é trabalho de outro teste;
este é a corda que avisa quando o chão sair.

**O que é falso aqui:** a chamada à OpenAI (emissão da credencial) e a geração
do embedding. Os dois custam dinheiro e rede, e nenhum dos dois é o que este
teste protege. O banco é de verdade, porque o SQL da busca é justamente o que
tem que continuar funcionando.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.ai.services.embedding_service import EmbeddingService
from src.ai.services.product_indexing import index_product
from src.api.dependencies.customer_auth import get_current_customer
from src.api.dependencies.database import get_db
from src.api.middleware.rate_limit_state import RateLimitStateMiddleware
from src.api.rate_limit import limiter
from src.models.ai_voice_session_model import AIVoiceSession
from src.api import voice as rota_de_voz
from src.ai.voice import realtime_client
from src.repositories.ai_repository import AIRepository
from tests.fabricas_db import (
    criar_categoria,
    criar_cliente,
    criar_configuracoes,
    criar_filial,
    criar_produto,
    criar_restaurante,
)


pytestmark = pytest.mark.db

VETOR = [0.1] * 1536
CREDENCIAL_FALSA = {
    "value": "ek_de_teste",
    "expires_at": 1786805373,
    "session": {"model": "gpt-realtime-mini"},
}


def test_a_voz_emite_credencial_e_a_ferramenta_devolve_produto(db, monkeypatch):
    restaurante = criar_restaurante(db, nome="Junior da Picanha")
    # Sem esta linha a emissão responde 403: a voz é ligada por restaurante e
    # nasce desligada em todo lugar.
    criar_configuracoes(db, restaurante, voice_enabled=True)
    # A filial explicita porque ela e parte do que este teste mede agora: a
    # busca da voz e por LOJA desde a revisao 20260820_0026.
    filial = criar_filial(db, restaurante)
    categoria = criar_categoria(db, restaurante, nome="Carnes", filial=filial)
    produto = criar_produto(
        db, restaurante, categoria, nome="Picanha na Chapa", preco=Decimal("23.90")
    )
    _indexar(db, produto, "Carnes")

    monkeypatch.setattr(EmbeddingService, "generate_embedding", lambda self, texto: list(VETOR))
    monkeypatch.setattr(realtime_client, "httpx", _openai_falsa())

    cliente = _cliente_com(db, criar_cliente(db))

    # 1. A credencial. Passa por `_get_active_restaurant` e
    #    `_build_restaurant_context`.
    sessao = cliente.post(
        "/voice/session",
        json={"restaurant_id": str(restaurante.id), "branch_id": str(filial.id)},
    )
    assert sessao.status_code == 200
    assert sessao.json()["credencial"]["value"] == "ek_de_teste"
    # Os tetos vêm do SERVIDOR junto com a credencial: página que escolhe o
    # próprio teto não é teto.
    assert sessao.json()["limites"]["duracao_maxima_s"] > 0
    # E a emissão virou linha no livro-razão — é dela que a cota e a
    # conciliação de custo dependem.
    assert db.get(AIVoiceSession, uuid.UUID(sessao.json()["sessao_id"])) is not None

    # 2. A ferramenta. Passa por `retrieval_service` e `_hydrate_products`.
    busca = cliente.post(
        "/voice/search",
        json={
            "restaurant_id": str(restaurante.id),
            "branch_id": str(filial.id),
            "consulta": "quero carne",
        },
    )
    assert busca.status_code == 200

    corpo = busca.json()
    assert [p["name"] for p in corpo["produtos"]] == ["Picanha na Chapa"]
    # O preço do cartão vem do banco, e o resumo que vai ao modelo usa o mesmo
    # formato do chat de texto — vírgula, não ponto.
    assert corpo["produtos"][0]["price"] == 23.90
    assert corpo["resumo"] == "Produtos encontrados: Picanha na Chapa - R$ 23,90"

    # 3. A OUTRA loja do mesmo restaurante nao conhece esta picanha.
    #
    # E o modo de falha que o passo 3 fechou, e o mais caro dos tres: na voz o
    # modelo FALA nome e preco em audio, e o cliente aceita de ouvido — nao ha
    # tela onde ele pudesse notar que aquilo e de outra loja.
    outra = criar_filial(db, restaurante, nome="Aldeota")
    db.flush()
    busca_na_outra = cliente.post(
        "/voice/search",
        json={
            "restaurant_id": str(restaurante.id),
            "branch_id": str(outra.id),
            "consulta": "quero carne",
        },
    )
    assert busca_na_outra.status_code == 200
    assert busca_na_outra.json()["produtos"] == []
    # E a negativa que volta ao modelo diz NESTA LOJA. A picanha existe no
    # restaurante — so nao aqui. O modelo repete o que le, e "nao temos"
    # dito sobre a rede inteira e uma frase que ninguem conserta depois.
    assert busca_na_outra.json()["resumo"] == "Nenhum produto encontrado nesta loja."


def _indexar(db, produto, category_name: str) -> None:
    index_product(
        repository=AIRepository(db),
        embedding_service=SimpleNamespace(generate_embedding=lambda texto: list(VETOR)),
        product=produto,
        category_name=category_name,
        source_updated_at=produto.updated_at,
    )
    db.flush()


def _cliente_com(db, cliente_logado) -> TestClient:
    """App mínimo com o router da voz. Não passa pelo `main.py`.

    O flag `VOICE_ENABLED` é lido no import do `main`, e o que este
    teste protege é o acoplamento com o `ChatService`, não o interruptor.

    O `limiter` e o `RateLimitStateMiddleware` vêm junto porque a rota de
    emissão é limitada: sem os dois, o wrapper do `@limiter.limit` lê
    `request.state.view_rate_limit` e estoura `AttributeError` — o 500 que o
    middleware existe para evitar.

    `get_current_customer` é sobrescrito em vez de assinar um JWT: o que este
    teste protege é o caminho da voz, e a autenticação de cliente já tem
    suíte própria.
    """
    app = FastAPI()
    app.state.limiter = limiter
    app.add_middleware(RateLimitStateMiddleware)
    app.include_router(rota_de_voz.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_customer] = lambda: cliente_logado
    return TestClient(app)


def _openai_falsa():
    """Substitui o módulo `httpx` dentro do `realtime_client`.

    `HTTPError` vai junto porque o `except` do serviço o referencia — sem ele,
    um erro no meio viraria `AttributeError` em vez do 503 que o código
    pretende.
    """
    def post(*_args, **_kwargs):
        return SimpleNamespace(status_code=200, json=lambda: CREDENCIAL_FALSA, text="")

    return SimpleNamespace(post=post, HTTPError=httpx.HTTPError)
