"""Cancela o pedido online cuja cobranca foi recusada e que ninguem retomou.

O pedido nasce ANTES da cobranca. Quando ela volta recusada, o pedido fica
gravado em `pending` com `payment_status='failed'` e **nada o move dali**: o
lojista nao consegue aceita-lo, a comanda de producao nao sai, e nenhuma
limpeza apaga pedido. O que ele continua fazendo, parado:

- **cobra comissao.** `billable_order_conditions` exclui `cancelled`,
  `rejected` e `refunded` — `pending` + `failed` passa nos tres, e
  `commission_amount` foi congelado na criacao. A plataforma cobra o lojista
  por um pedido que ninguem pagou e ninguem preparou;
- **segura o cupom e o cashback.** As duas redencoes acontecem dentro do
  `create_order` e so voltam nos estados de REVERSING_STATUSES. O cliente
  cujo cartao foi recusado perdeu o cupom e o saldo num pedido que nao
  existe — e nao consegue usar o cupom de novo se refizer o carrinho;
- **trava a exclusao de conta.** `_ensure_no_order_in_flight` recusa com
  409 e a frase "tente novamente quando eles forem concluidos". Este pedido
  nunca conclui: e recusa permanente vestida de temporaria, num caminho de
  LGPD.

**Por que uma varredura e nao um cancelamento no veredito.** `failed` nao e
o fim da linha: `PAYMENT_STATUS_TRANSITIONS` permite `failed -> pending /
in_review / paid`, `PAYABLE_STATUSES` aceita `failed`, e a chave de
idempotencia por tentativa (armadilha 6) existe exatamente para a segunda
cobranca do mesmo pedido nascer limpa. O cliente cujo cartao foi recusado
pega o segundo cartao na carteira. Cancelar no instante da recusa trocaria
"recusado, tente outro cartao" por "seu pedido foi cancelado" — e
`cancelled` e terminal, entao nao ha volta: ele refaz o carrinho inteiro.

Dai a CARENCIA de 30 minutos. No pix, `failed` so chega quando o QR expira
(o cliente ja teve os 30 minutos dele e nao pagou); no cartao, a segunda
tentativa vem em segundos. O relogio e `orders.updated_at`, que o
`attach_payment_intent` move — entao cada tentativa nova reinicia a
carencia, que e o comportamento certo.

**`cancelled` e nao `rejected`.** Ninguem recusou. `rejected` aparece para o
cliente como "o restaurante recusou seu pedido", que seria mentira; o motivo
real vai no `note` do historico.

**A escrita passa por `OrderStatusChangeService`**, como as outras tres
portas de cancelamento (armadilha 25.1). E o que devolve o cupom, devolve o
cashback e grava o historico. De quebra, o estorno que ele chama nao gasta
nada: `failed` esta fora de `PAYMENT_STATUSES_WITH_LIVE_CHARGE`, entao o
`PaymentRefundService` olha o pedido e nao faz chamada nenhuma ao gateway.
**Esta varredura custa zero requisicao externa.**

**Sai sempre com codigo 0 quando o banco respondeu**, como o
`estorna_pedidos_cancelados.py` ao lado: pedido que nao resolve nao pode
derrubar o processo, senao o laco do container gira em cima dele para
sempre.

**A janela de `--desde-dias` nao alcanca o passivo que ja existe.** Ela e a
mesma defesa da varredura irma: um erro no WHERE cancela 90 dias, nao a
historia inteira. Para drenar o acumulado de uma vez, uma execucao manual
com a janela maior — depois de olhar o `--dry-run`:

    docker exec pedeaqui-api python scripts/cancela_pedidos_sem_pagamento.py \\
        --desde-dias 3650 --dry-run

Uso:

    python scripts/cancela_pedidos_sem_pagamento.py
    python scripts/cancela_pedidos_sem_pagamento.py --dry-run
    python scripts/cancela_pedidos_sem_pagamento.py --carencia-minutos 60
"""

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.repositories.order_repository import OrderRepository  # noqa: E402
from src.db.session import SessionLocal  # noqa: E402
from src.services.order_status_change_service import OrderStatusChangeService  # noqa: E402
from src.utils.security import utcnow  # noqa: E402


logger = logging.getLogger("uvicorn.error")

# Quanto tempo o pedido recusado tem para ser retomado antes de a varredura
# desistir dele. O erro caro e o numero CURTO: o pedido vira terminal e o
# cliente que estava pegando o segundo cartao refaz o carrinho.
DEFAULT_GRACE_MINUTES = 30

# Quanto para tras varrer. Ver o docstring do modulo: a janela e defesa
# contra um erro no WHERE, nao regra de negocio.
DEFAULT_LOOKBACK_DAYS = 90

# Teto por execucao. O laco roda de novo em 15 min, e drenar uma fila grande
# em varias passadas e melhor que segurar uma transacao por muito tempo.
BATCH_LIMIT = 200

# Como o cancelamento assina `order_status_history.changed_by`. Nao e pessoa
# nem lojista, e o historico precisa dizer isso: quem le a tela de um pedido
# cancelado tem que conseguir separar "o restaurante cancelou" de "o
# pagamento nunca foi concluido".
SYSTEM_SIGNATURE = "sistema"

# O que aparece no historico e, por tabela, na tela do cliente.
CANCEL_REASON = "Cancelado automaticamente: o pagamento não foi concluído"

# O "de onde veio" no escopo da idempotencia. `OrderStatusChangeService`
# monta o escopo com ele, e uma varredura nao tem rota — mas tem identidade,
# e ela precisa ser diferente das tres rotas para nao colidir com elas.
SWEEP_ROUTE = "sweep cancela_pedidos_sem_pagamento"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="lista o que seria cancelado, sem gravar nada",
    )
    parser.add_argument(
        "--carencia-minutos",
        type=int,
        default=DEFAULT_GRACE_MINUTES,
        help=(
            "quanto tempo desde a ultima tentativa de pagamento antes de desistir "
            f"(padrao: {DEFAULT_GRACE_MINUTES})"
        ),
    )
    parser.add_argument(
        "--desde-dias",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"quantos dias para tras varrer (padrao: {DEFAULT_LOOKBACK_DAYS})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    agora = utcnow()
    with SessionLocal() as db:
        abandonados = OrderRepository(db).list_orders_abandoned_after_payment_failure(
            older_than=agora - timedelta(minutes=args.carencia_minutos),
            since=agora - timedelta(days=args.desde_dias),
            limit=BATCH_LIMIT,
        )
        # Copiado para tuplas ANTES de qualquer commit: o service commita, e
        # a partir dai cada atributo lido de um objeto da lista dispararia um
        # SELECT novo.
        alvos = [(order.id, order.restaurant_id, order.order_number) for order in abandonados]

        if args.dry_run:
            return _report_dry_run(alvos)
        return _cancel_all(db, alvos)


def _report_dry_run(alvos: list[tuple]) -> int:
    print(f"[pagamento-nao-concluido] {len(alvos)} pedido(s) seriam cancelados")
    for _order_id, _restaurant_id, order_number in alvos:
        print(f"[pagamento-nao-concluido]   pedido #{order_number}")
    return 0


def _cancel_all(db, alvos: list[tuple]) -> int:
    cancelados = 0
    for order_id, restaurant_id, order_number in alvos:
        if _cancel_one(db, order_id, restaurant_id, order_number):
            cancelados += 1

    print(f"[pagamento-nao-concluido] {len(alvos)} pedido(s) lidos, {cancelados} cancelado(s)")
    # Zero mesmo com pendencia: ver o docstring do modulo.
    return 0


def _cancel_one(db, order_id, restaurant_id, order_number: int) -> bool:
    """Cancela um pedido, sem poder derrubar a varredura inteira.

    A releitura nao e cerimonia: entre o SELECT da lista e esta linha o
    cliente pode ter voltado e pago. `ensure_payment_allows_order_status`
    NAO barraria isso — ela libera `cancelled` em qualquer estado de
    pagamento, de proposito, porque cancelar e a saida para o pagamento que
    nao chegou. Sem esta reconferencia, a varredura cancelaria um pedido
    recem-pago e o estorno o desfaria em seguida, cobrando a tarifa do
    gateway do lojista por um pedido que estava certo.
    """
    order = OrderRepository(db).get_order_detail(order_id, restaurant_id)
    if order is None:
        return False
    if order.status != "pending" or order.payment_status != "failed":
        logger.info(
            "[pagamento-nao-concluido] pedido #%s mudou desde a leitura "
            "(status=%s payment_status=%s); deixando como esta",
            order_number,
            order.status,
            order.payment_status,
        )
        return False

    try:
        OrderStatusChangeService(db).apply(
            order=order,
            restaurant_id=restaurant_id,
            new_status="cancelled",
            note=CANCEL_REASON,
            changed_by=SYSTEM_SIGNATURE,
            # O escopo da idempotencia leva o PEDIDO, como no cancelamento
            # pelo cliente: a varredura nao e um requisitante com identidade
            # propria, e o pedido ja identifica um alvo so.
            requester=f"sistema:{order_id}",
            route=SWEEP_ROUTE,
            # A rota nao existe, entao nao ha chave para reusar. O segundo
            # passe encontra o pedido ja em `cancelled` e nem chega aqui — a
            # consulta so devolve `pending`.
            idempotency_key=None,
        )
    except Exception:
        logger.exception(
            "[pagamento-nao-concluido] falha ao cancelar pedido #%s order_id=%s",
            order_number,
            order_id,
        )
        return False

    logger.info("[pagamento-nao-concluido] pedido #%s cancelado", order_number)
    return True


if __name__ == "__main__":
    raise SystemExit(main())
