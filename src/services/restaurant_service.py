from collections import defaultdict
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.restaurant_model import Restaurant
from src.repositories.branch_repository import BranchRepository
from src.repositories.restaurant_repository import RestaurantRepository
from src.schemas.restaurant_schema import (
    BranchAddressResponse,
    BranchPaymentMethodResponse,
    BusinessHourDayResponse,
    BusinessHourPeriodResponse,
    PaymentMethodsResponse,
    RestaurantInfoBranchResponse,
    RestaurantInfoResponse,
    RestaurantInfoRestaurantResponse,
    RestaurantPublicResponse,
)
from src.utils.storage import build_storage_url


class RestaurantService:
    TIMEZONE = "America/Fortaleza"
    DAY_LABELS = (
        "Segunda-feira",
        "Terça-feira",
        "Quarta-feira",
        "Quinta-feira",
        "Sexta-feira",
        "Sábado",
        "Domingo",
    )

    def __init__(self, db: Session):
        self.restaurant_repository = RestaurantRepository(db)
        self.branch_repository = BranchRepository(db)

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

    def get_detailed_public_info(
        self,
        restaurant_slug: str,
        branch_id: UUID | None = None,
    ) -> RestaurantInfoResponse:
        restaurant = self.get_active_restaurant(restaurant_slug)
        if branch_id is not None:
            branch = self.branch_repository.get_active_by_id_and_restaurant(
                branch_id, restaurant.id
            )
        else:
            # Mesma escolha da estimativa de entrega desde que
            # `get_default_branch` passou a existir. O comportamento desta
            # rota nao muda (a listagem ja vinha ordenada por `is_main`); o
            # que muda e a regra ter UM lugar.
            branch = self.branch_repository.get_default_branch(restaurant.id)

        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Filial não encontrada para este restaurante",
            )

        hours = self.branch_repository.list_business_hours(branch.id)
        payment_methods = self.branch_repository.list_enabled_payment_methods(branch.id)
        now = datetime.now(ZoneInfo(self.TIMEZONE))

        return RestaurantInfoResponse(
            restaurant=RestaurantInfoRestaurantResponse(
                id=restaurant.id,
                name=restaurant.name,
                logo_url=build_storage_url(restaurant.logo_path),
            ),
            branch=RestaurantInfoBranchResponse(
                id=branch.id,
                name=branch.name,
                display_name=branch.display_name,
                email=branch.email,
                phone=branch.phone,
                whatsapp=branch.whatsapp,
                address=self._build_address(branch),
            ),
            business_hours=self._group_business_hours(hours),
            payment_methods=self._group_payment_methods(payment_methods),
            current_weekday=now.weekday(),
            current_day_label=self.DAY_LABELS[now.weekday()],
        )

    @classmethod
    def _group_business_hours(cls, hours) -> list[BusinessHourDayResponse]:
        grouped = defaultdict(list)
        for item in hours:
            grouped[item.weekday].append(item)

        result = []
        for weekday in sorted(grouped):
            items = grouped[weekday]
            periods = [
                BusinessHourPeriodResponse(
                    opens_at=item.opens_at.strftime("%H:%M"),
                    closes_at=item.closes_at.strftime("%H:%M"),
                )
                for item in items
                if not item.is_closed and item.opens_at is not None and item.closes_at is not None
            ]
            result.append(
                BusinessHourDayResponse(
                    weekday=weekday,
                    day_label=cls.DAY_LABELS[weekday],
                    periods=periods,
                    is_closed=not periods,
                )
            )
        return result

    @staticmethod
    def _group_payment_methods(methods) -> PaymentMethodsResponse:
        grouped = {"online": [], "delivery": []}
        for method in methods:
            grouped[method.payment_flow].append(
                BranchPaymentMethodResponse(
                    id=method.id,
                    payment_flow=method.payment_flow,
                    method_type=method.method_type,
                    brand=method.brand,
                    label=method.label,
                    icon_key=method.icon_key,
                    enabled=method.enabled,
                    requires_gateway=method.requires_gateway,
                )
            )
        return PaymentMethodsResponse(**grouped)

    @staticmethod
    def _build_address(branch) -> BranchAddressResponse:
        street = branch.address_street or branch.address
        number = branch.address_number
        neighborhood = branch.address_neighborhood or branch.neighborhood
        city = branch.address_city or branch.city
        state = branch.address_state or branch.state
        zipcode = branch.address_zipcode or branch.zipcode

        street_and_number = ", ".join(value for value in (street, number) if value)
        city_and_state = " - ".join(value for value in (city, state) if value)
        full_address = " - ".join(
            value for value in (street_and_number, neighborhood, city_and_state, zipcode) if value
        )
        return BranchAddressResponse(
            street=street,
            number=number,
            neighborhood=neighborhood,
            city=city,
            state=state,
            zipcode=zipcode,
            full_address=full_address,
        )

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
