"""O "deu erro" do lojista, virado registro que da para reproduzir.

## O que isto resolve

Antes: o lojista mandava "deu erro" no WhatsApp e a investigacao comecava por
descobrir qual erro, em que tela, em qual loja e com qual usuario. Agora o
relato chega com a historia, o log que a tela capturou, a tela e — quando o
erro tem um pedido — o numero dele. Restaurante, filial e usuario nao sao
perguntados: eles saem do token.

## Dado pessoal: o que e mascarado e o que NAO e

**Credencial e sempre mascarada, antes do INSERT.** `Authorization`, JWT,
`Idempotency-Key`, `tracking_token` e campos de senha. O motivo nao e
privacidade: e que um token de painel colado num relato vira um token de
painel gravado em texto puro numa tabela que ninguem audita — e quem lesse a
tabela abriria o painel do restaurante. Um `tracking_token` ali abre o pedido
de um cliente pelo mesmo caminho.

**Nome, telefone e endereco que escaparem para o texto livre ficam como o
lojista escreveu.** Foi decisao, e tem dois motivos:

- regex de nome e endereco acerta metade e da confianca falsa. "Cliente da
  Praca do Ferreira, 200" nao tem forma nenhuma;
- o relato sem o contexto que o lojista escreveu nao reproduz nada, e o custo
  de nao reproduzir e a investigacao que este arquivo existe para encurtar.

O que compensa isso e o `order_number`: com um campo estruturado apontando o
pedido, o lojista nao PRECISA escrever "o pedido do Joao, telefone 91 9…" —
e o que escapar assim mesmo tem prazo.

## A retencao E o mecanismo de exclusao (armadilha 38)

A tabela nao pende de `customers`: a exclusao de conta nunca vai alcanca-la,
porque o texto e escrito por um lojista sobre um cliente que nem sabe que este
registro existe. Noventa dias, varridos pelo container `limpeza` junto com as
outras cinco tabelas de `scripts/cleanup_idempotency_keys.py`.

Esticar esse prazo troca o mecanismo de exclusao por espaco em disco. E a
decisao que o numero esconde.
"""

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.repositories.admin_error_report_repository import AdminErrorReportRepository
from src.schemas.admin_error_report_schema import (
    CreateErrorReportRequest,
    ErrorReportResponse,
)


logger = logging.getLogger("uvicorn.error")


# Por quantos dias o relato fica no banco. Ver o cabecalho deste arquivo.
ERROR_REPORT_RETENTION_DAYS = 90


# O que entra no lugar da credencial. Uma marca visivel, e nao o sumico do
# trecho: quem le o relato precisa saber que havia um token ali — senao o
# proximo passo da investigacao e procurar por que a requisicao nao tinha
# Authorization.
MARCA = "[redigido]"

# Cabecalho inteiro, quando o log vem com os headers da requisicao.
_CABECALHO_SECRETO = re.compile(
    r"(?im)^(authorization|idempotency-key|x-webhook-signature|cookie)(\s*:\s*).*$"
)

# Campo de JSON cujo NOME denuncia o conteudo. `tracking_token` cai aqui pelo
# sufixo `token`, e e o caso mais provavel de todos: o painel cola a resposta
# inteira do pedido no relato.
_CAMPO_SECRETO = re.compile(
    r'(?i)("(?:[a-z_]*(?:password|senha|secret|token|signature)[a-z_]*)"\s*:\s*")[^"]*(")'
)

# `Bearer eyJ...` solto no meio do texto, fora de cabecalho e fora de JSON.
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")

# JWT nu: tres blocos base64url separados por ponto, o primeiro comecando em
# `eyJ`. E o formato do token do painel, entao ele e reconhecivel colado
# sozinho.
#
# **O `eyJ` nao e enfeite.** Sem ele, "tres blocos separados por ponto" casa
# com `payment_gateway.mercadopago.create_payment` — ou seja, com a linha de
# traceback que o lojista mais tem a colar aqui, que sairia do relato inteira
# como `[redigido]`. `eyJ` e o base64 de `{"`, e todo JWT comeca por ele.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

# O token de acompanhamento dentro da URL em que ele aparece. Fora dela nao
# ha como reconhece-lo: `token_urlsafe(32)` nao tem forma nenhuma que o
# distinga de um hash, de um id ou de um pedaco de base64 de imagem — e uma
# regra que apagasse "toda sequencia longa" comeria justamente o log.
_TRACKING_NA_URL = re.compile(r"(?i)(/orders/track/)[A-Za-z0-9_-]{16,}")


def error_report_retention_cutoff(now: datetime) -> datetime:
    """Antes deste instante, o relato tem que sair.

    Mesmo prazo e mesmo motivo do `ai_feedback`
    (`chat_service.feedback_retention_cutoff`): texto livre sobre gente, numa
    tabela sem `customer_id`, sem titular que consiga pedir a exclusao. Noventa
    dias cobrem o bug que volta todo mes; um relato de quatro meses atras nao
    e reproduzivel de qualquer forma, porque o codigo mudou.
    """
    return now - timedelta(days=ERROR_REPORT_RETENTION_DAYS)


def redigir_credenciais(texto: str) -> str:
    """Mascara o que abriria uma porta, e deixa o resto como foi escrito.

    A ordem importa em um ponto: o cabecalho e tratado ANTES do JWT, senao a
    linha `Authorization: Bearer eyJ…` perderia so o token e manteria o
    cabecalho pela metade, o que se le como se o valor tivesse sido curto.

    Nao mascara nome, telefone nem endereco — o cabecalho do modulo explica
    por que, e a retencao e o que cobre o que escapa.
    """
    redigido = _CABECALHO_SECRETO.sub(rf"\1\2{MARCA}", texto)
    redigido = _CAMPO_SECRETO.sub(rf"\g<1>{MARCA}\g<2>", redigido)
    redigido = _BEARER.sub(f"Bearer {MARCA}", redigido)
    redigido = _JWT.sub(MARCA, redigido)
    return _TRACKING_NA_URL.sub(rf"\g<1>{MARCA}", redigido)


def _redigir_opcional(texto: str | None) -> str | None:
    if texto is None:
        return None
    return redigir_credenciais(texto)


class AdminErrorReportService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = AdminErrorReportRepository(db)

    def create(
        self,
        scope: AdminScope,
        payload: CreateErrorReportRequest,
    ) -> ErrorReportResponse:
        """Grava o relato e devolve o comprovante.

        **Nada e recusado por conteudo.** O `order_number` nao e conferido
        contra `orders` de proposito (ver o model): ele e um numero que uma
        pessoa digitou olhando para a tela, e recusar o relato porque ele nao
        casa seria recusa-lo exatamente na hora em que ele importa.

        `scope.branch_id` nulo entra nulo: significa "o relato nao aponta uma
        loja", que e a verdade para o dono e para quem enxerga a rede inteira.
        """
        relato = self.repository.create(
            restaurant_id=scope.restaurant_id,
            branch_id=scope.branch_id,
            admin_user_id=scope.admin_user.id,
            description=redigir_credenciais(payload.description),
            error_log=_redigir_opcional(payload.error_log),
            screen=payload.screen,
            order_number=payload.order_number,
        )
        try:
            self.db.commit()
            self.db.refresh(relato)
        except Exception:
            self.db.rollback()
            raise

        # A linha que faz o relato novo aparecer sem ninguem ir olhar. Leva o
        # id, a loja e a tela, e NAO leva o texto: o texto e justamente o
        # campo que pode ter dado pessoal dentro, e log nao carrega dado
        # pessoal neste projeto. Quem quiser ler o relato usa
        # `scripts/error_reports.py`.
        logger.info(
            "[relato] erro relatado pelo lojista id=%s restaurant_id=%s "
            "branch_id=%s screen=%s order_number=%s",
            relato.id,
            scope.restaurant_id,
            scope.branch_id,
            payload.screen or "-",
            payload.order_number or "-",
        )
        return ErrorReportResponse.model_validate(relato)
