import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.dependencies.customer_auth import get_optional_current_customer
from src.api.dependencies.database import get_db
from src.models.customer_model import Customer
from src.schemas.delivery_schema import DeliveryEstimateRequest, DeliveryEstimateResponse
from src.services.delivery_estimate_service import DeliveryEstimateService


router = APIRouter(prefix="/restaurants", tags=["delivery"])
logger = logging.getLogger("uvicorn.error")


@router.post(
    "/{restaurant_slug}/delivery/estimate",
    response_model=DeliveryEstimateResponse,
)
def estimate_delivery(
    restaurant_slug: str,
    payload: DeliveryEstimateRequest,
    current_customer: Customer | None = Depends(get_optional_current_customer),
    db: Session = Depends(get_db),
) -> DeliveryEstimateResponse:
    logger.info(
        "[Delivery contract] route=delivery_estimate address_id_present=%s "
        "address_present=%s branch_id_empty=%s",
        str(payload.address_id is not None).lower(),
        str(payload.address is not None).lower(),
        str(payload.branch_id is None).lower(),
    )
    result = DeliveryEstimateService(db).estimate(
        restaurant_slug,
        payload,
        current_customer,
    )
    return result.to_response()
