"""As rotas do atendimento por voz. Nenhuma sobe sem `VOICE_ENABLED`.

    GET  /voice/test                       bancada de teste
    POST /voice/session                    emite a credencial efemera
    POST /voice/session/{id}/connected     o navegador reporta o call_id
    POST /voice/session/{id}/ended         o navegador reporta o fim
    POST /voice/search                     a ferramenta que o modelo chama

Fica ao lado de `src/api/chat.py` de proposito: sao os dois agentes da mesma
casa, um por texto e outro por voz, e a simetria da rota e a primeira coisa
que quem chega procura.

A bancada e servida por AQUI, e nao aberta como arquivo do disco, por um
motivo pratico: `file://` tem origem `null`, e o CORS desta API exige origem
conhecida com credenciais. Servida na propria origem, ela nao depende de
mexer na lista de origens do `main.py`.
"""

import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.dependencies.customer_auth import get_current_customer
from src.api.dependencies.database import get_db
from src.api.rate_limit import VOICE_SESSION_RATE_LIMIT, limiter
from src.core.config import settings
from src.models.customer_model import Customer
from src.ai.voice.search_service import VoiceSearchService
from src.ai.voice.session_service import VoiceSessionService
from src.services.chat_service import ChatService


router = APIRouter(prefix="/voice", tags=["experimento"])

PAGINA = Path(__file__).resolve().parent / "pagina.html"


class SessaoRequest(BaseModel):
    restaurant_id: uuid.UUID


class ConexaoRequest(BaseModel):
    call_id: str = Field(min_length=1, max_length=200)


class EncerramentoRequest(BaseModel):
    motivo: str = Field(min_length=1, max_length=200)


class BuscaRequest(BaseModel):
    restaurant_id: uuid.UUID
    consulta: str = Field(min_length=1, max_length=500)
    # `gt=0` porque teto zero ou negativo esvazia a busca inteira, e o modelo
    # e quem preenche este campo.
    preco_maximo: Decimal | None = Field(default=None, gt=0)


@router.get("/test", response_class=HTMLResponse)
def pagina() -> HTMLResponse:
    """Lida do disco a cada requisicao, de proposito: da para editar o HTML e
    dar F5 sem reiniciar a API."""
    return HTMLResponse(PAGINA.read_text(encoding="utf-8"))


@router.post("/session")
@limiter.limit(VOICE_SESSION_RATE_LIMIT)
def criar_sessao(
    request: Request,
    payload: SessaoRequest,
    db: Session = Depends(get_db),
    cliente: Customer = Depends(get_current_customer),
) -> dict:
    """Credencial efemera para o navegador abrir a sessao de audio.

    EXIGE CLIENTE AUTENTICADO. Audio anonimo e consumo sem ninguem a quem
    cobrar, a quem limitar e a quem bloquear: sem login, um laco de `curl`
    emite credenciais ate a fatura chegar. `get_current_customer` responde 401
    com "Token ausente" quando nao vem Bearer.

    As tres barreiras desta rota, da mais externa para a mais interna:
    rate limit por IP (contra quem troca de conta), cota por cliente e por
    restaurante (em `VoiceSessionService`), e o teto de duracao gravado na
    sessao.
    """
    chat_service = ChatService(db, agent="/voz")
    restaurant = chat_service._get_active_restaurant(payload.restaurant_id)
    restaurant_context = chat_service._build_restaurant_context(restaurant)

    sessao, credencial = VoiceSessionService(db).abrir(
        restaurant_id=restaurant.id,
        customer_id=cliente.id,
        restaurant_context=restaurant_context,
    )

    # Os tetos viajam junto com a credencial, e nao ficam escritos no HTML: e o
    # SERVIDOR quem decide quanto tempo a sessao pode durar. Uma pagina que
    # escolhe o proprio teto nao e teto nenhum — o que sustenta o numero do
    # lado de ca esta em `sessao_control_service.py`.
    return {
        "sessao_id": str(sessao.id),
        "credencial": credencial,
        "limites": {
            "duracao_maxima_s": settings.VOICE_MAX_SESSION_SECONDS,
            "inatividade_s": settings.VOICE_IDLE_SECONDS,
            "aviso_antes_s": settings.VOICE_WARN_BEFORE_END_SECONDS,
        },
    }


@router.post("/session/{sessao_id}/connected")
def registrar_conexao(
    sessao_id: uuid.UUID,
    payload: ConexaoRequest,
    db: Session = Depends(get_db),
) -> dict:
    """O navegador informa o `call_id` que leu do cabecalho `Location`.

    E a unica ponte entre a linha do livro-razao e a chamada de verdade na
    OpenAI — sem ela o servidor nao consegue desligar aquela sessao. Ver o
    cabecalho de `sessao_control_service.py` para o que isso implica.
    """
    encontrou = VoiceSessionService(db).registrar_conexao(sessao_id, payload.call_id)
    return {"registrado": encontrou}


@router.post("/session/{sessao_id}/ended")
def registrar_encerramento(
    sessao_id: uuid.UUID,
    payload: EncerramentoRequest,
    db: Session = Depends(get_db),
) -> dict:
    """O navegador avisa que encerrou, e por que.

    O servidor desliga na OpenAI mesmo assim quando tem `call_id`: o cliente
    reporta o que ELE fez, e a conexao pode ter sobrevivido do outro lado.
    """
    encerrou = VoiceSessionService(db).encerrar(sessao_id, payload.motivo)
    return {"encerrado": encerrou}


@router.post("/search")
def buscar(payload: BuscaRequest, db: Session = Depends(get_db)) -> dict:
    """A ferramenta. O navegador chama isto quando o modelo pede uma busca.

    Devolve os dois formatos de uma vez: `produtos` vai para a TELA (objeto
    completo, com preco e imagem do banco) e `resumo` volta para o MODELO
    (so nome e preco). Separar os dois e o que impede o modelo de falar um
    preco que o cartao nao mostra — ele nunca ve outro numero.
    """
    service = VoiceSearchService(db)
    produtos = service.buscar(payload.restaurant_id, payload.consulta, payload.preco_maximo)

    return {
        "produtos": produtos,
        "resumo": service.resumo_para_o_modelo(produtos),
    }
