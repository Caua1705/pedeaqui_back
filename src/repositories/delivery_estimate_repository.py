from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.models.delivery_estimate_model import DeliveryEstimate


class DeliveryEstimateRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, estimate: DeliveryEstimate) -> DeliveryEstimate:
        self.db.add(estimate)
        self.db.flush()
        return estimate

    def get_valid_by_token(self, token: str, now: datetime) -> DeliveryEstimate | None:
        # A expiracao entra na consulta e nao no service porque uma linha
        # vencida nao deve nem ser lida: o preco dela nao vale mais.
        stmt = select(DeliveryEstimate).where(
            DeliveryEstimate.token == token,
            DeliveryEstimate.expires_at > now,
        )
        return self.db.scalar(stmt)

    def delete_expired(self, now: datetime) -> int:
        result = self.db.execute(
            delete(DeliveryEstimate).where(DeliveryEstimate.expires_at <= now)
        )
        return result.rowcount or 0
