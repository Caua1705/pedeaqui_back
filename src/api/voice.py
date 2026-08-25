"""As rotas do atendimento por voz. Nenhuma sobe sem `VOICE_ENABLED`.

    POST /voice/session                    emite a credencial efemera
    POST /voice/session/{id}/connected     o navegador reporta o call_id
    POST /voice/session/{id}/ended         o navegador reporta o fim
    POST /voice/search                     a ferramenta que o modelo chama

Fica ao lado de `src/api/chat.py` de proposito: sao os dois agentes da mesma
casa, um por texto e outro por voz, e a simetria da rota e a primeira coisa
que quem chega procura.

NAO HA MAIS BANCADA SERVIDA DAQUI. `GET /voice/test` existiu e foi removida em
24/08/2026: o HTML dela morava no repositorio, foi apagado por engano em
`bd1f164`, e a rota passou a responder 500 lendo um arquivo que nao existia.
A bancada viva e externa (`bancada-assistente/bancada.html`, servida em
`localhost:5500`), e as portas 5500, 5501 e 5173 ja estao na lista de origens
do `main.py` — nao ha nada a servir daqui.
"""

import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from src.api.dependencies.customer_auth import get_current_customer
from src.api.dependencies.database import get_db
from src.api.rate_limit import VOICE_SESSION_RATE_LIMIT, limiter
from src.core.config import settings
from src.models.customer_model import Customer
from src.ai.voice.realtime_client import VOZES_DO_REALTIME
from src.ai.voice.search_service import VoiceSearchService
from src.ai.voice.session_service import UsoReportado, VoiceSessionService
from src.ai.voice.voice_prompt import branch_context_for, saudacao_para
from src.services.chat_service import ChatService


logger = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/voice", tags=["experimento"])


class SessaoRequest(BaseModel):
    restaurant_id: uuid.UUID
    # OBRIGATORIO desde a revisao 20260820_0026, pelo mesmo motivo do `/chat`
    # — ver `ChatRequest`. Aqui ele e conferido ANTES de a credencial ser
    # emitida: uma sessao de voz custa dinheiro por minuto, e nao vale abrir
    # uma para descobrir na primeira busca que a loja nao existe.
    branch_id: uuid.UUID
    # A VOZ, E ELA E DE BANCADA (24/08/2026). Producao nao manda este campo, e
    # o padrao continua sendo `VOICE_NAME` do `.env`.
    #
    # Ele existe porque escolher timbre exige OUVIR, e `VOICE_NAME` so muda
    # com restart do processo (`settings` e construido no import). Com o campo,
    # a bancada troca no `<select>`, aperta Falar, e ouve.
    #
    # Duas travas, e as duas precisam ser verdade para a escolha valer:
    # a lista fechada aqui embaixo, e `VOICE_ALLOW_VOICE_OVERRIDE` na rota.
    # A validacao fica no SCHEMA porque assim o nome errado morre em 422 antes
    # de a rota varrer vencidas, contar duas cotas e pagar uma emissao.
    voz: str | None = None

    @field_validator("voz")
    @classmethod
    def _voz_conhecida(cls, valor: str | None) -> str | None:
        if valor is None or valor in VOZES_DO_REALTIME:
            return valor
        raise ValueError(f"voz desconhecida; use uma de: {', '.join(VOZES_DO_REALTIME)}")


class ConexaoRequest(BaseModel):
    call_id: str = Field(min_length=1, max_length=200)


class EncerramentoRequest(UsoReportado):
    """O motivo, que sempre existiu, mais os contadores de consumo.

    Herda de `UsoReportado` para os seis campos de numero terem UMA definicao
    so — a mesma que o service recebe. O corpo continua plano: `motivo` e os
    seis contadores no mesmo nivel.
    """

    motivo: str = Field(min_length=1, max_length=200)


class BuscaRequest(BaseModel):
    restaurant_id: uuid.UUID
    # OBRIGATORIO, e conferido de novo aqui apesar de a sessao ja ter sido
    # aberta com uma filial: esta rota nao recebe o id da sessao, entao nao
    # ha de onde herdar a loja. E o mesmo regime que o `restaurant_id` deste
    # corpo ja tinha.
    branch_id: uuid.UUID
    consulta: str = Field(min_length=1, max_length=500)
    # `gt=0` porque teto zero ou negativo esvazia a busca inteira, e o modelo
    # e quem preenche este campo.
    preco_maximo: Decimal | None = Field(default=None, gt=0)
    # `ordenar` SAIU DAQUI em 25/08/2026, e a ausencia e o desenho. Ele era um
    # enum que o MODELO preenchia; hoje quem le "mais barato" na consulta e
    # decide a ordenacao e `_reescrever_consulta`, no backend. Um campo a menos
    # no corpo e uma decisao a menos no modelo.
    #
    # Nao ha compatibilidade a manter e nao ha 422 novo: o Pydantic ignora
    # campo desconhecido por padrao, entao bancada que ainda mande `ordenar`
    # continua funcionando — e o valor dela deixa de ter efeito, que e o ponto.


class CategoriasRequest(BaseModel):
    """Restaurante e loja, e mais nada.

    Sem parametro do modelo de proposito: nao ha o que ele possa preencher
    errado. A lista de categorias de uma filial e a mesma para qualquer forma
    de perguntar, e um campo aqui — um filtro, um termo — seria mais uma chance
    de ele acertar a pergunta e errar o argumento, que e o modo de falha que o
    enum de `ordenar` foi desenhado para nao ter.
    """

    restaurant_id: uuid.UUID
    branch_id: uuid.UUID


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
    # A filial deixou de ser so uma barreira e virou DADO: e dela que sai o
    # contexto que o modelo recebe. O retorno de `_get_active_branch` era
    # descartado quando a checagem era o unico proposito da chamada.
    branch = chat_service._get_active_branch(restaurant.id, payload.branch_id)

    voz = _voz_da_sessao(payload.voz)
    sessao, credencial = VoiceSessionService(db).abrir(
        restaurant_id=restaurant.id,
        customer_id=cliente.id,
        restaurant_context=chat_service._build_restaurant_context(restaurant),
        branch_context=branch_context_for(branch),
        voz=voz,
    )

    # Os tetos viajam junto com a credencial, e nao ficam escritos no HTML: e o
    # SERVIDOR quem decide quanto tempo a sessao pode durar. Uma pagina que
    # escolhe o proprio teto nao e teto nenhum — o que sustenta o numero do
    # lado de ca esta em `sessao_control_service.py`.
    return {
        "sessao_id": str(sessao.id),
        "credencial": credencial,
        # A frase que o atendente fala SOZINHO, antes de o cliente falar. Ela
        # vem daqui, e nao do prompt, por duas razoes que estao no cabecalho
        # de `voice_prompt.py` — o nome quebraria o cache do prefixo, e frase
        # pronta dentro do prompt vira molde (armadilha 44).
        #
        # Ela NAO vai para o log: e o primeiro nome do cliente, e log deste
        # repositorio nao leva dado pessoal.
        "saudacao": saudacao_para(cliente.name),
        # A voz que REALMENTE valeu, e nao a que foi pedida. Com o seletor
        # desligado, quem pediu `sage` recebe `marin` e precisa saber disso —
        # senao a conclusao da bancada e "o seletor nao funciona" quando o que
        # aconteceu foi a chave estar desligada, que e um estado legitimo.
        "voz": voz or settings.VOICE_NAME,
        "limites": {
            "duracao_maxima_s": settings.VOICE_MAX_SESSION_SECONDS,
            "inatividade_s": settings.VOICE_IDLE_SECONDS,
            "aviso_antes_s": settings.VOICE_WARN_BEFORE_END_SECONDS,
        },
    }


def _voz_da_sessao(pedida: str | None) -> str | None:
    """A voz escolhida no corpo, ou `None` para cair no `VOICE_NAME`.

    O nome ja veio da lista fechada (`SessaoRequest`); o que falta decidir e se
    a chave da bancada esta ligada. Desligada, a escolha e IGNORADA e nao vira
    erro: o campo e de teste, e derrubar a emissao de quem mandou um campo a
    mais seria transformar experimento em incidente. Quem precisa saber o que
    valeu le o `voz` da resposta.
    """
    if pedida is None:
        return None
    if settings.VOICE_ALLOW_VOICE_OVERRIDE:
        return pedida

    logger.info(
        "[Voz] voz pedida no corpo e ignorada: seletor desligado "
        "| pedida=%s | valendo=%s",
        pedida,
        settings.VOICE_NAME,
    )
    return None


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
    """O navegador avisa que encerrou, e por que — e quanto consumiu.

    O servidor desliga na OpenAI mesmo assim quando tem `call_id`: o cliente
    reporta o que ELE fez, e a conexao pode ter sobrevivido do outro lado.

    Os contadores de token sao OPCIONAIS e nenhum deles muda o encerramento.
    Eles existem porque o consumo so aparece no evento `response.done`, que
    chega ao navegador — o backend nao ve a conversa e sem este corpo nao teria
    como saber quanto uma sessao custou. O front soma os eventos da sessao e
    manda o total; quem nao mandar encerra do mesmo jeito.

    `payload` ja E um `UsoReportado` (o schema herda dele), entao ele vai
    inteiro para o service sem conversao no meio.
    """
    encerrou = VoiceSessionService(db).encerrar(sessao_id, payload.motivo, payload)
    return {"encerrado": encerrou}


@router.post("/search")
def buscar(payload: BuscaRequest, db: Session = Depends(get_db)) -> dict:
    """A ferramenta. O navegador chama isto quando o modelo pede uma busca.

    Devolve os dois formatos de uma vez: `produtos` vai para a TELA (objeto
    completo, com preco e imagem do banco) e `resumo` volta para o MODELO.
    Separar os dois e o que impede o modelo de falar um preco que o cartao nao
    mostra — ele nunca ve outro numero.

    O `resumo` MUDOU DE FORMA em 25/08/2026: era uma lista de ate cinco linhas
    para o modelo recortar, e agora vem em dois blocos rotulados — a FRASE
    pronta para ser dita (com o teto de dois e a regra do preco ja aplicados
    aqui) e os DADOS para responder pergunta de seguimento. Ver
    `VoiceSearchService.resultado_para_o_modelo`.

    BUSCA VAZIA PASSA A LEVAR AS CATEGORIAS JUNTO, e essa e a segunda decisao
    que sai do modelo: "so busque um termo mais amplo se a palavra dele nao
    devolver nada" era regra de prompt para uma coisa que o backend sabe
    sozinho. A consulta extra so acontece no caminho vazio, nao gasta embedding
    (e SQL agregado por filial) e evita o turno em que o cliente ouve so "aqui
    nao temos" e desliga.

    Os dois ja saem recortados pela filial do corpo. A negativa diz "nesta
    loja" por isso: o produto que nao esta aqui pode estar na outra.
    """
    service = VoiceSearchService(db)
    produtos = service.buscar(
        payload.restaurant_id,
        payload.branch_id,
        payload.consulta,
        payload.preco_maximo,
    )

    categorias = None
    if not produtos:
        categorias = service.listar_categorias(payload.restaurant_id, payload.branch_id)

    return {
        "produtos": produtos,
        "resumo": service.resultado_para_o_modelo(produtos, categorias),
    }


@router.post("/categories")
def listar_categorias(payload: CategoriasRequest, db: Session = Depends(get_db)) -> dict:
    """A segunda ferramenta. O que a loja tem, por categoria, sem busca.

    Existe porque "quais sao as categorias?" e "o que voces tem?" nao tem
    resposta na busca por significado: nenhuma das duas se parece com prato
    nenhum. Em 25/08/2026 a primeira foi respondida com um cardapio inventado.

    Devolve so `resumo`, e NAO manda nada para a tela. E a diferenca desta rota
    para a `/search`: categoria nao e produto, nao tem preco nem imagem, e um
    cartao de "Carnes" nao e uma coisa que o cliente possa tocar para adicionar.
    O fluxo desenhado e categoria primeiro (falada), produto depois (na tela,
    pela busca que vem em seguida).
    """
    service = VoiceSearchService(db)
    categorias = service.listar_categorias(payload.restaurant_id, payload.branch_id)
    return {"resumo": service.resumo_das_categorias(categorias)}
