"""Remove chaves, estimativas e codigos de verificacao que ja nao servem.

Rodar por cron. Apagar linha vencida NAO afeta correcao: passado o TTL a
chave ja nao protege mais nada, e reenviar um pedido de 24h atras deve
mesmo criar um pedido novo. Vale o mesmo para `delivery_estimates`: depois
do TTL a estimativa nao pode mais ser reaproveitada, entao a linha so ocupa
espaco. O objetivo aqui e so nao deixar as tabelas crescerem para sempre.

Os codigos de verificacao entraram na frente 5 (LGPD) por um motivo
diferente dos outros dois: o que aquelas duas tabelas acumulavam para sempre
era o E-MAIL de todo mundo que ja pediu um codigo, muito depois de o codigo
ter deixado de servir para qualquer coisa. Nao e espaco em disco, e dado
pessoal sem prazo.

E eles NAO sao cortados pelo `expires_at`, ao contrario dos outros dois. A
linha continua valendo depois de o codigo vencer — para o teto de reenvios e
para o token de reset — e quem sabe ate quando e
`auth_service.codes_retention_cutoff`, que explica as duas janelas.

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
from src.models.customer_model import EmailVerificationCode, PasswordResetCode
from src.models.delivery_estimate_model import DeliveryEstimate
from src.models.idempotency_key_model import IdempotencyKey
from src.repositories.delivery_estimate_repository import DeliveryEstimateRepository
from src.repositories.customer_repository import CustomerRepository
from src.repositories.idempotency_repository import IdempotencyRepository
from src.services.auth_service import codes_retention_cutoff
from src.utils.security import utcnow


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove chaves, estimativas e codigos de verificacao vencidos."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apenas conta o que seria apagado, sem apagar.",
    )
    args = parser.parse_args()

    now = utcnow()
    cutoff = codes_retention_cutoff(now)
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
            codes = sum(
                db.scalar(
                    select(func.count()).select_from(modelo).where(modelo.created_at < cutoff)
                )
                for modelo in (EmailVerificationCode, PasswordResetCode)
            )
            print(
                f"[dry-run] {keys} chave(s), {estimates} estimativa(s) e "
                f"{codes} codigo(s) seriam removidos."
            )
            return 0

        removed_keys = IdempotencyRepository(db).delete_expired(now=now)
        removed_estimates = DeliveryEstimateRepository(db).delete_expired(now)
        removed_codes = CustomerRepository(db).delete_codes_created_before(cutoff)
        db.commit()
        print(
            f"{removed_keys} chave(s), {removed_estimates} estimativa(s) e "
            f"{removed_codes} codigo(s) removidos."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
