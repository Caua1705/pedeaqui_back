import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from src.api.dependencies.customer_auth import get_optional_current_customer
from src.api.dependencies.database import get_db
from src.api.rate_limit import DELIVERY_ESTIMATE_RATE_LIMIT, limiter
from src.models.customer_model import Customer
from src.schemas.delivery_schema import DeliveryEstimateRequest, DeliveryEstimateResponse
from src.services.delivery_estimate_service import DeliveryEstimateService


router = APIRouter(prefix="/restaurants", tags=["delivery"])
logger = logging.getLogger("uvicorn.error")


@router.post(
    "/{restaurant_slug}/delivery/estimate",
    response_model=DeliveryEstimateResponse,
)
# Cada chamada pode virar geocode + rota no Google, que sao pagos, e grava uma
# linha em `delivery_estimates`. Rota publica: login e OPCIONAL aqui, entao
# nao ha conta para responsabilizar — o limite por IP e a unica barreira.
@limiter.limit(DELIVERY_ESTIMATE_RATE_LIMIT)
def estimate_delivery(
    request: Request,
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
    # estimate_and_store e nao estimate: aqui a estimativa e GUARDADA, e o
    # token devolvido junto e o que evita que a criacao do pedido refaca
    # geocode + rota no Google minutos depois.
    result, stored = DeliveryEstimateService(db).estimate_and_store(
        restaurant_slug,
        payload,
        current_customer,
    )
    response = result.to_response()
    if stored is not None:
        response.estimate_token = stored.token
        response.estimate_expires_at = stored.expires_at
    return response
