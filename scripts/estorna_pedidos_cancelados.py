"""Retenta o estorno dos pedidos cancelados que ficaram com dinheiro preso.

**Esta varredura NAO e o mecanismo de estorno.** Quem estorna e o
`PaymentRefundService`, chamado na hora — pelo painel quando o lojista
cancela, e pelo webhook quando o pagamento entra num pedido ja recusado. Este
script existe para o que aquela tentativa nao alcanca: o Mercado Pago fora do
ar no minuto do cancelamento, um timeout de dez segundos, uma credencial
recusada que o lojista so foi corrigir no dia seguinte.

Por que ele NAO mora no container `limpeza`, que ja roda scripts assim:
aquele lacinho dorme 24h entre execucoes, e o que ele faz e apagar linha
vencida — um dia de atraso ali nao custa nada. Aqui o atraso e **dinheiro de
cliente parado na conta do restaurante**, e a cadencia certa e de minutos.
Encaixar os dois no mesmo laco significaria escolher uma cadencia errada para
um dos dois.

**Nao ha coluna de fila, e isso e desenho.** O conjunto pendente e uma
CONSULTA: pedido `cancelled`/`rejected`, com `payment_flow='online'` e
`payment_status` ainda em pending/in_review/paid. Assim que o estorno (ou o
cancelamento da cobranca) e aplicado, o pedido sai do conjunto sozinho. Uma
coluna "estorno pendente" seria um segundo lugar guardando a mesma verdade, e
o primeiro a sair de sincronia num rollback. Ver
`OrderRepository.list_orders_awaiting_refund`.

**Repetir e seguro, e e a razao de a chave de idempotencia do estorno ser
fixa.** Um estorno que ja aconteceu, retentado, devolve o mesmo estorno em
vez de um segundo — ver `payment_gateway.refund_payment`. Uma cobranca ja
cancelada, retentada, toma 4xx do gateway; a execucao seguinte le o estado
real, encontra `failed` e so sincroniza a copia local. As duas falhas se
curam sozinhas.

**Sai sempre com codigo 0 quando o banco respondeu.** Pedido que nao resolve
— restaurante sem credencial, prazo de estorno vencido no gateway — nao pode
derrubar o processo: o laco do container reiniciaria em 5 minutos e ficaria
girando em cima do mesmo pedido para sempre. O que ele deixa e um warning por
pedido, com o mesmo texto de sempre no radar:

    sem estorno automatico

Uso:

    python scripts/estorna_pedidos_cancelados.py
    python scripts/estorna_pedidos_cancelados.py --dry-run
    python scripts/estorna_pedidos_cancelados.py --desde-dias 7

No container:

    docker exec pedeaqui-api python scripts/estorna_pedidos_cancelados.py --dry-run
"""

import argparse
import logging
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.db.session import SessionLocal  # noqa: E402
from src.repositories.order_repository import OrderRepository  # noqa: E402
from src.services.payment_refund_service import PaymentRefundService  # noqa: E402
from src.utils.security import utcnow  # noqa: E402


logger = logging.getLogger("uvicorn.error")

# Quanto para tras a varredura olha. O Mercado Pago aceita estorno por um
# prazo limitado depois da aprovacao; passado ele, retentar nao devolve
# dinheiro nenhum e o conserto e humano (falar com o suporte deles), nao uma
# tentativa a mais de madrugada.
#
# O numero e generoso de proposito: e melhor gastar uma chamada por dia num
# pedido irrecuperavel — e ver o warning dele — do que deixa-lo sumir da
# varredura em silencio no trigesimo dia.
DEFAULT_LOOKBACK_DAYS = 90

# Teto de pedidos por execucao. Cada um custa de uma a duas chamadas ao
# gateway, e o laco roda de novo em minutos: e melhor drenar uma fila grande
# em varias passadas do que segurar uma execucao por meia hora.
BATCH_LIMIT = 200


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="lista o que seria estornado, sem falar com o gateway",
    )
    parser.add_argument(
        "--desde-dias",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"quantos dias para tras varrer (padrao: {DEFAULT_LOOKBACK_DAYS})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with SessionLocal() as db:
        pendentes = OrderRepository(db).list_orders_awaiting_refund(
            since=utcnow() - timedelta(days=args.desde_dias),
            limit=BATCH_LIMIT,
        )
        # Copiado para tuplas ANTES de qualquer commit: o service commita, e
        # a partir dai cada atributo lido de um objeto da lista dispararia um
        # SELECT novo.
        alvos = [
            (order.id, order.restaurant_id, order.order_number, order.payment_status)
            for order in pendentes
        ]

        if args.dry_run:
            return _report_dry_run(alvos)
        return _refund_all(db, alvos)


def _report_dry_run(alvos: list[tuple]) -> int:
    print(f"[estorno] {len(alvos)} pedido(s) com cobranca viva e pedido terminal")
    for _order_id, _restaurant_id, order_number, payment_status in alvos:
        print(f"[estorno]   pedido #{order_number} payment_status={payment_status}")
    return 0


def _refund_all(db, alvos: list[tuple]) -> int:
    contagem: Counter = Counter()
    for order_id, restaurant_id, order_number, payment_status in alvos:
        outcome = PaymentRefundService(db).refund_terminal_order(order_id, restaurant_id)
        contagem[outcome.action] += 1
        if not outcome.resolved:
            # O service ja gritou com o texto do radar quando a falha foi do
            # gateway. Esta linha acrescenta o numero do pedido, que e por
            # onde uma pessoa consegue ir atras dele no painel.
            logger.warning(
                "[estorno] pedido #%s continua com dinheiro preso payment_status=%s desfecho=%s (%s)",
                order_number,
                payment_status,
                outcome.action,
                outcome.detail or "-",
            )

    print(f"[estorno] {len(alvos)} pedido(s) processado(s): {dict(contagem)}")
    # Zero mesmo com pendencia: ver o docstring do modulo.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
