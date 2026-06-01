from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.restaurant_model import Restaurant
from src.repositories.restaurant_repository import RestaurantRepository
from src.schemas.restaurant_schema import RestaurantPublicResponse
from src.utils.storage import build_storage_url


class RestaurantService:
    def __init__(self, db: Session):
        self.restaurant_repository = RestaurantRepository(db)

    def get_active_restaurant(self, restaurant_slug: str) -> Restaurant:
        restaurant = self.restaurant_repository.get_active_by_slug(restaurant_slug)
        if not restaurant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Restaurante não encontrado",
            )
        return restaurant

    def get_public_info(self, restaurant_slug: str) -> RestaurantPublicResponse:
        restaurant = self.get_active_restaurant(restaurant_slug)
        return self.to_public_response(restaurant)

    @staticmethod
    def to_public_response(restaurant: Restaurant) -> RestaurantPublicResponse:
        return RestaurantPublicResponse(
            id=restaurant.id,
            name=restaurant.name,
            slug=restaurant.slug,
            description=restaurant.description,
            logo_path=restaurant.logo_path,
            logo_url=build_storage_url(restaurant.logo_path),
            cover_path=restaurant.cover_path,
            cover_url=build_storage_url(restaurant.cover_path),
            primary_color=restaurant.primary_color,
            secondary_color=restaurant.secondary_color,
            is_active=restaurant.is_active,
        )
