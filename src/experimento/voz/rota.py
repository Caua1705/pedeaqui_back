"""As tres rotas do experimento. Nenhuma sobe sem `EXPERIMENTO_VOZ_ENABLED`.

    GET  /experimento/voz          a pagina de teste
    POST /experimento/voz/sessao   emite a credencial efemera
    POST /experimento/voz/buscar   a ferramenta que o modelo chama

A pagina e servida por AQUI, e nao aberta como arquivo do disco, por um
motivo pratico: `file://` tem origem `null`, e o CORS desta API exige origem
conhecida com credenciais. Servida na propria origem, o experimento nao
depende de mexer na lista de origens do `main.py`.
"""

import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.core.config import settings
from src.experimento.voz.busca_service import VozBuscaService
from src.experimento.voz.sessao_service import emitir_credencial_efemera
from src.services.chat_service import ChatService


router = APIRouter(prefix="/experimento/voz", tags=["experimento"])

PAGINA = Path(__file__).resolve().parent / "pagina.html"


class SessaoRequest(BaseModel):
    restaurant_id: uuid.UUID


class BuscaRequest(BaseModel):
    restaurant_id: uuid.UUID
    consulta: str = Field(min_length=1, max_length=500)
    # `gt=0` porque teto zero ou negativo esvazia a busca inteira, e o modelo
    # e quem preenche este campo.
    preco_maximo: Decimal | None = Field(default=None, gt=0)


@router.get("", response_class=HTMLResponse)
def pagina() -> HTMLResponse:
    """Lida do disco a cada requisicao, de proposito: da para editar o HTML e
    dar F5 sem reiniciar a API."""
    return HTMLResponse(PAGINA.read_text(encoding="utf-8"))


@router.post("/sessao")
def criar_sessao(payload: SessaoRequest, db: Session = Depends(get_db)) -> dict:
    """Credencial efemera para o navegador abrir a sessao de audio.

    SEM LOGIN E SEM COTA. Ver o cabecalho de `sessao_service.py` para a lista
    do que falta antes de isto existir fora da maquina de quem esta testando.
    """
    chat_service = ChatService(db, agent="/voz")
    restaurant = chat_service._get_active_restaurant(payload.restaurant_id)
    restaurant_context = chat_service._build_restaurant_context(restaurant)

    credencial = emitir_credencial_efemera(restaurant.id, restaurant_context)

    # Os tetos viajam junto com a credencial, e nao ficam escritos no HTML: e o
    # SERVIDOR quem decide quanto tempo a sessao pode durar. Uma pagina que
    # escolhe o proprio teto nao e teto nenhum — mas ver o BLOCO 2 para o que
    # este numero vale contra um cliente que nao coopera.
    return {
        "credencial": credencial,
        "limites": {
            "duracao_maxima_s": settings.VOZ_DURACAO_MAXIMA_SEGUNDOS,
            "inatividade_s": settings.VOZ_INATIVIDADE_SEGUNDOS,
            "aviso_antes_s": settings.VOZ_AVISO_ANTES_DE_ENCERRAR_SEGUNDOS,
        },
    }


@router.post("/buscar")
def buscar(payload: BuscaRequest, db: Session = Depends(get_db)) -> dict:
    """A ferramenta. O navegador chama isto quando o modelo pede uma busca.

    Devolve os dois formatos de uma vez: `produtos` vai para a TELA (objeto
    completo, com preco e imagem do banco) e `resumo` volta para o MODELO
    (so nome e preco). Separar os dois e o que impede o modelo de falar um
    preco que o cartao nao mostra — ele nunca ve outro numero.
    """
    service = VozBuscaService(db)
    produtos = service.buscar(payload.restaurant_id, payload.consulta, payload.preco_maximo)

    return {
        "produtos": produtos,
        "resumo": service.resumo_para_o_modelo(produtos),
    }
