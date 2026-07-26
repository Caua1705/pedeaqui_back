"""Remove chaves de idempotencia vencidas.

Rodar por cron. Apagar linha vencida NAO afeta correcao: passado o TTL a
chave ja nao protege mais nada, e reenviar um pedido de 24h atras deve
mesmo criar um pedido novo. O objetivo aqui e so nao deixar a tabela
crescer para sempre.

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
from src.models.idempotency_key_model import IdempotencyKey
from src.repositories.idempotency_repository import IdempotencyRepository
from src.utils.security import utcnow


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove chaves de idempotencia vencidas.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apenas conta o que seria apagado, sem apagar.",
    )
    args = parser.parse_args()

    now = utcnow()
    with SessionLocal() as db:
        if args.dry_run:
            total = db.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.expires_at <= now)
            )
            print(f"[dry-run] {total} chave(s) vencida(s) seriam removidas.")
            return 0

        removed = IdempotencyRepository(db).delete_expired(now=now)
        db.commit()
        print(f"{removed} chave(s) vencida(s) removida(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
