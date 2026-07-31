"""Remove chaves de idempotencia e estimativas de entrega vencidas.

Rodar por cron. Apagar linha vencida NAO afeta correcao: passado o TTL a
chave ja nao protege mais nada, e reenviar um pedido de 24h atras deve
mesmo criar um pedido novo. Vale o mesmo para `delivery_estimates`: depois
do TTL a estimativa nao pode mais ser reaproveitada, entao a linha so ocupa
espaco. O objetivo aqui e so nao deixar as tabelas crescerem para sempre.

Uso:

    python scripts/cleanup_idempotency_keys.py
    python scripts/cleanup_idempotency_keys.py --dry-run

No container:

    docker exec pedeaqui-api python scripts/cleanup_idempotency_keys.py
"""

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import func, select

from src.db.session import SessionLocal
from src.models.delivery_estimate_model import DeliveryEstimate
from src.models.idempotency_key_model import IdempotencyKey
from src.repositories.delivery_estimate_repository import DeliveryEstimateRepository
from src.repositories.idempotency_repository import IdempotencyRepository
from src.utils.security import utcnow


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove chaves de idempotencia e estimativas de entrega vencidas."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apenas conta o que seria apagado, sem apagar.",
    )
    args = parser.parse_args()

    now = utcnow()
    with SessionLocal() as db:
        if args.dry_run:
            keys = db.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.expires_at <= now)
            )
            estimates = db.scalar(
                select(func.count())
                .select_from(DeliveryEstimate)
                .where(DeliveryEstimate.expires_at <= now)
            )
            print(f"[dry-run] {keys} chave(s) e {estimates} estimativa(s) seriam removidas.")
            return 0

        removed_keys = IdempotencyRepository(db).delete_expired(now=now)
        removed_estimates = DeliveryEstimateRepository(db).delete_expired(now)
        db.commit()
        print(f"{removed_keys} chave(s) e {removed_estimates} estimativa(s) removida(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
