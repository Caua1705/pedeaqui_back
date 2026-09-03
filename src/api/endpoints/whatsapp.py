"""O webhook do WhatsApp. UM endereco para a aplicacao inteira.

A Meta manda tudo para o mesmo lugar, de todos os numeros de todos os
restaurantes, e o roteamento e pelo `phone_number_id` que vem no proprio
corpo. Um webhook por restaurante seria o desenho caro de desfazer: o
endereco fica cadastrado no painel da Meta de cada Business Manager, e mudar
significa passar em todas elas.

Duas rotas, e as duas sao da Meta — nenhuma e chamada por app nem por painel:

- `GET`  a verificacao, que acontece UMA VEZ, no clique de "Verificar e
         salvar" do painel. Ela devolve o `hub.challenge` como TEXTO PURO;
- `POST` as mensagens e os status de entrega.

Sem rate limit, pelo mesmo motivo do webhook de pagamento: a Meta reenvia em
rajada quando volta de uma queda, e devolver 429 para ela e perder aviso. A
protecao aqui e a assinatura.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.core.config import settings
from src.schemas.whatsapp_schema import WhatsAppWebhookResponse
from src.services.whatsapp_webhook_service import WhatsAppWebhookService


logger = logging.getLogger("uvicorn.error")

router = APIRouter(tags=["whatsapp"])

# O unico valor que a Meta manda no `hub.mode` da verificacao.
_MODO_DE_ASSINATURA = "subscribe"


@router.get(
    "/webhooks/whatsapp",
    response_class=PlainTextResponse,
    responses={
        403: {"description": "Verify token diferente do configurado"},
        503: {"description": "WHATSAPP_WEBHOOK_VERIFY_TOKEN nao configurada"},
    },
)
def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> PlainTextResponse:
    """A verificacao que a Meta faz ao salvar o webhook no painel.

    **Devolve o `hub.challenge` como texto puro, e nao como JSON.** A Meta
    compara byte a byte com o que mandou; `"1158201444"` com aspas nao e
    `1158201444`, e o painel so diz que nao conseguiu validar — sem dizer o
    que estava errado.

    Os tres codigos sao distintos de proposito, porque respondem a coisas
    diferentes quando esse painel nao ajuda:

    - **503**: a variavel nao esta no ambiente. O problema e nosso, e o
      conserto e subir a API com ela ANTES de clicar em salvar;
    - **403**: o token nao bate. O que foi colado no painel e outro;
    - **200**: pronto, e nao se repete — a verificacao acontece uma vez.

    `compare_digest` e nao `!=`: e segredo, e a armadilha 18 vale aqui como
    em qualquer outro.
    """
    esperado = settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN
    if not esperado:
        logger.error(
            "[WhatsApp] verificacao do webhook recusada: "
            "WHATSAPP_WEBHOOK_VERIFY_TOKEN nao configurada"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WHATSAPP_WEBHOOK_VERIFY_TOKEN não configurada",
        )

    if hub_mode != _MODO_DE_ASSINATURA or not hmac.compare_digest(hub_verify_token, esperado):
        logger.warning("[WhatsApp] verificacao do webhook com token invalido")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Verify token inválido"
        )

    return PlainTextResponse(hub_challenge)


@router.post("/webhooks/whatsapp", response_model=WhatsAppWebhookResponse)
async def receive_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> WhatsAppWebhookResponse:
    """Mensagem recebida e status de entrega, de qualquer numero.

    `async def` para conseguir o corpo CRU: a assinatura e calculada sobre os
    bytes exatos que a Meta enviou, e reserializar o JSON (espacos, ordem das
    chaves) quebraria a conferencia. Como o resto do projeto e sincrono, o
    service roda em threadpool para nao segurar o event loop enquanto fala
    com o banco.
    """
    raw_body = await request.body()
    headers = dict(request.headers)
    resultado = await run_in_threadpool(
        lambda: WhatsAppWebhookService(db).handle(raw_body=raw_body, headers=headers)
    )
    return WhatsAppWebhookResponse(**resultado)
