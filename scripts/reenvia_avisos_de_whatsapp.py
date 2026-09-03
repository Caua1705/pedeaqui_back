"""Reenvia o aviso de WhatsApp que falhou por algo que passa.

Um timeout da Meta no instante do aceite custava, ate aqui, o aviso inteiro
daquele pedido: virava uma linha `failed` em `whatsapp_messages` e ninguem
retentava. O cliente nao tinha como saber, e nos tambem nao — o unico registro
era uma linha de tabela que ninguem le.

## O que ela retenta, e por que a lista nao esta aqui

Ela le `next_attempt_at`, e **nao** `status='failed'` nem o `error_code`.

Quem sabe se repetir tem chance e o TIPO da excecao, no instante da falha:
`WhatsAppTransportError.retryable` e `True`, `WhatsAppRejectedError` e `False`
(armadilha 49). O service grava a decisao na coluna; esta varredura obedece.

Fosse ao contrario — uma lista de codigos retentaveis aqui — a mesma pergunta
teria duas respostas em dois arquivos, e a segunda ficaria para tras. O caso
concreto e o `132001`: template nao aprovado. Ele parece transitorio (a
aprovacao chega em horas), e retenta-lo a cada dois minutos gastaria a
validade inteira do aviso sem uma chance sequer, porque quem aprova template e
uma pessoa no painel da Meta.

## As tres desistencias moram no service, nao aqui

`WhatsAppOrderNotifier.retry` decide sozinho se ainda vale mandar: validade
vencida, pedido cancelado/recusado, canal sem numero utilizavel. Sao regra de
DOMINIO, e uma varredura e agendamento — se elas morassem aqui, um segundo
chamador (um botao de "reenviar" no painel, um dia) teria que lembrar de
repeti-las, que e a armadilha 46.

## Sai sempre com codigo 0 quando o banco respondeu

Como o `estorna_pedidos_cancelados.py` e o `cancela_pedidos_sem_pagamento.py`
ao lado: aviso que nao sai nao pode derrubar o processo, senao o laco do
container gira em cima dele para sempre. A Meta fora do ar por dez minutos e
exatamente o cenario para o qual esta varredura existe — ela nao pode ser
tambem o que a derruba.

Uso:

    python scripts/reenvia_avisos_de_whatsapp.py
    python scripts/reenvia_avisos_de_whatsapp.py --dry-run
"""

import argparse
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.core.config import settings  # noqa: E402
from src.db.session import SessionLocal  # noqa: E402
from src.repositories.whatsapp_repository import WhatsAppMessageRepository  # noqa: E402
from src.services.whatsapp_notification_service import (  # noqa: E402
    REENVIO_DESISTIU,
    REENVIO_ENVIADO,
    WhatsAppOrderNotifier,
)
from src.utils.security import utcnow  # noqa: E402


logger = logging.getLogger("uvicorn.error")

# Teto por execucao. O laco roda de novo em 2 min, e drenar em varias passadas
# e melhor que segurar uma transacao aberta durante dezenas de chamadas HTTP a
# Meta — cada linha aqui custa uma requisicao externa.
BATCH_LIMIT = 50


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="lista o que seria reenviado, sem mandar nada e sem gravar",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not settings.WHATSAPP_NOTIFICATIONS_ENABLED:
        # A mesma chave que cala o aviso ao vivo cala o reenvio. Duas chaves
        # fariam "desliguei o WhatsApp" continuar mandando mensagem por aqui.
        print("[reenvio-whatsapp] WHATSAPP_NOTIFICATIONS_ENABLED desligado; nada a fazer")
        return 0

    with SessionLocal() as db:
        pendentes = WhatsAppMessageRepository(db).list_due_for_retry(
            now=utcnow(), limit=BATCH_LIMIT
        )

        if args.dry_run:
            return _report_dry_run(pendentes)
        return _retry_all(db, pendentes)


def _report_dry_run(pendentes: list) -> int:
    print(f"[reenvio-whatsapp] {len(pendentes)} aviso(s) seriam reenviados")
    for mensagem in pendentes:
        print(
            f"[reenvio-whatsapp]   pedido_id={mensagem.order_id} aviso={mensagem.kind} "
            f"tentativas={mensagem.attempts} ultimo_erro={mensagem.error_code}"
        )
    return 0


def _retry_all(db, pendentes: list) -> int:
    notifier = WhatsAppOrderNotifier(db)
    desfechos = {REENVIO_ENVIADO: 0, REENVIO_DESISTIU: 0}

    for mensagem in pendentes:
        desfecho = _retry_one(notifier, mensagem)
        if desfecho in desfechos:
            desfechos[desfecho] += 1

    print(
        f"[reenvio-whatsapp] {len(pendentes)} lido(s), "
        f"{desfechos[REENVIO_ENVIADO]} enviado(s), "
        f"{desfechos[REENVIO_DESISTIU]} desistido(s)"
    )
    # Zero mesmo com aviso pendente: ver o docstring do modulo.
    return 0


def _retry_one(notifier: WhatsAppOrderNotifier, mensagem) -> str | None:
    """Um aviso, sem poder derrubar a varredura inteira.

    O `except` largo e o mesmo de `OrderStatusChangeService`: o inesperado num
    aviso nao pode custar os outros quarenta e nove da fila. A linha fica como
    esta — `next_attempt_at` no passado — e a proxima passada a pega de novo,
    que e o comportamento certo para um erro que ninguem previu.
    """
    try:
        return notifier.retry(mensagem)
    except Exception:
        logger.exception(
            "[reenvio-whatsapp] erro inesperado no reenvio pedido_id=%s aviso=%s",
            mensagem.order_id,
            mensagem.kind,
        )
        notifier.db.rollback()
        return None


if __name__ == "__main__":
    raise SystemExit(main())
