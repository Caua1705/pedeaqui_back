from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.schemas.restaurant_schema import RestaurantPublicResponse
from src.services.restaurant_service import RestaurantService


router = APIRouter(prefix="/restaurants", tags=["restaurants"])


@router.get("/{restaurant_slug}", response_model=RestaurantPublicResponse)
def get_restaurant_public_info(
    restaurant_slug: str,
    db: Session = Depends(get_db),
) -> RestaurantPublicResponse:
    return RestaurantService(db).get_public_info(restaurant_slug)
