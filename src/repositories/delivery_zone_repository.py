import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.delivery_zone_model import DeliveryZone


class DeliveryZoneRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_active_by_neighborhood(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID,
        neighborhood: str,
    ) -> DeliveryZone | None:
        stmt = select(DeliveryZone).where(
            DeliveryZone.restaurant_id == restaurant_id,
            DeliveryZone.branch_id == branch_id,
            DeliveryZone.is_active.is_(True),
            func.lower(DeliveryZone.neighborhood) == neighborhood.strip().lower(),
        )
        return self.db.scalar(stmt)
