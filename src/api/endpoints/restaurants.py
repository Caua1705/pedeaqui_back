from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.schemas.restaurant_schema import RestaurantInfoResponse, RestaurantPublicResponse
from src.services.restaurant_service import RestaurantService


router = APIRouter(prefix="/restaurants", tags=["restaurants"])


@router.get("/{restaurant_slug}", response_model=RestaurantPublicResponse)
def get_restaurant_public_info(
    restaurant_slug: str,
    db: Session = Depends(get_db),
) -> RestaurantPublicResponse:
    return RestaurantService(db).get_public_info(restaurant_slug)


@router.get("/{restaurant_slug}/info", response_model=RestaurantInfoResponse)
def get_restaurant_detailed_public_info(
    restaurant_slug: str,
    branch_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> RestaurantInfoResponse:
    return RestaurantService(db).get_detailed_public_info(restaurant_slug, branch_id)
