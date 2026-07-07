import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, time as datetime_time
from decimal import Decimal
from time import monotonic, time
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.config import settings
from src.integrations.google_maps_routes_client import (
    Coordinates,
    GoogleMapsLocationNotFoundError,
    GoogleMapsRoutesClient,
    GoogleMapsUnavailableError,
)
from src.models.customer_model import Customer
from src.repositories.branch_repository import BranchRepository
from src.repositories.customer_repository import CustomerRepository
from src.repositories.delivery_zone_repository import DeliveryZoneRepository
from src.repositories.menu_repository import MenuRepository
from src.schemas.delivery_schema import DeliveryEstimateRequest, DeliveryEstimateResponse
from src.services.restaurant_service import RestaurantService
from src.utils.money import money_to_float


logger = logging.getLogger("uvicorn.error")
DELIVERY_TIMEZONE = ZoneInfo("America/Fortaleza")


@dataclass
class DeliveryEstimateResult:
    serviceable: bool
    reason: str | None
    message: str | None
    distance_km: float | None
    travel_time_min: int | None
    prep_time_min: int | None
    prep_time_max: int | None
    eta_min: int | None
    eta_max: int | None
    delivery_fee: float | None
    provider: str
    fallback: bool
    latitude: float | None
    longitude: float | None

    def to_response(self) -> DeliveryEstimateResponse:
        values = asdict(self)
        values.pop("latitude")
        values.pop("longitude")
        return DeliveryEstimateResponse(**values)


class DeliveryEstimateCache:
    _memory: dict[str, tuple[float, str]] = {}
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.redis = None
        if settings.REDIS_URL:
            try:
                import redis

                self.redis = redis.Redis.from_url(
                    settings.REDIS_URL,
                    decode_responses=True,
                    socket_timeout=1,
                    socket_connect_timeout=1,
                )
            except Exception:
                logger.warning("[Delivery cache] redis_initialization_failed=true")

    def get(self, key: str) -> DeliveryEstimateResult | None:
        payload = None
        if self.redis is not None:
            try:
                payload = self.redis.get(key)
            except Exception:
                logger.warning("[Delivery cache] redis_read_failed=true")
        if payload is None:
            now = monotonic()
            with self._lock:
                entry = self._memory.get(key)
                if entry and entry[0] > now:
                    payload = entry[1]
                elif entry:
                    del self._memory[key]
        if payload is None:
            return None
        try:
            return DeliveryEstimateResult(**json.loads(payload))
        except (TypeError, ValueError):
            return None

    def set(self, key: str, result: DeliveryEstimateResult) -> None:
        ttl = (
            settings.DELIVERY_ESTIMATE_CACHE_TTL_SECONDS
            if result.serviceable
            else settings.DELIVERY_ESTIMATE_NEGATIVE_CACHE_TTL_SECONDS
        )
        payload = json.dumps(asdict(result), separators=(",", ":"))
        if self.redis is not None:
            try:
                self.redis.setex(key, ttl, payload)
            except Exception:
                logger.warning("[Delivery cache] redis_write_failed=true")
        with self._lock:
            self._memory[key] = (monotonic() + ttl, payload)


class DeliveryEstimateService:
    def __init__(
        self,
        db: Session,
        maps_client: GoogleMapsRoutesClient | None = None,
        cache: DeliveryEstimateCache | None = None,
    ) -> None:
        self.restaurant_service = RestaurantService(db)
        self.branch_repository = BranchRepository(db)
        self.customer_repository = CustomerRepository(db)
        self.delivery_zone_repository = DeliveryZoneRepository(db)
        self.menu_repository = MenuRepository(db)
        self.maps_client = maps_client or GoogleMapsRoutesClient(
            api_key=settings.GOOGLE_MAPS_ROUTES_API_KEY,
            base_url=settings.GOOGLE_MAPS_ROUTES_BASE_URL,
            timeout_seconds=settings.GOOGLE_MAPS_TIMEOUT_SECONDS,
            routing_preference=settings.GOOGLE_MAPS_ROUTING_PREFERENCE,
        )
        self.cache = cache or DeliveryEstimateCache()

    def estimate(
        self,
        restaurant_slug: str,
        payload: DeliveryEstimateRequest,
        current_customer: Customer | None,
    ) -> DeliveryEstimateResult:
        logger.info(
            "[Delivery estimate debug] event=start restaurant_slug=%s branch_id=%s "
            "address_id_present=%s inline_address_present=%s",
            restaurant_slug,
            payload.branch_id,
            str(payload.address_id is not None).lower(),
            str(payload.address is not None).lower(),
        )
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        restaurant_settings = self.menu_repository.get_settings(restaurant.id)
        if restaurant_settings and restaurant_settings.accepts_delivery is False:
            result = self._not_serviceable("delivery_disabled", "Restaurante nao aceita entrega.")
            self._log_final_result(result)
            return result

        branch = self._resolve_branch(restaurant.id, payload.branch_id)
        if getattr(branch, "accepts_delivery", True) is False:
            result = self._not_serviceable("delivery_disabled", "Filial nao aceita entrega.")
            self._log_final_result(result)
            return result
        logger.info(
            "[Delivery estimate debug] event=restaurant_branch_resolved "
            "restaurant_id=%s branch_id=%s branch_name=%s branch_latitude=%s branch_longitude=%s",
            restaurant.id,
            branch.id,
            getattr(branch, "name", None) or getattr(branch, "display_name", None),
            getattr(branch, "latitude", None),
            getattr(branch, "longitude", None),
        )
        address = self._resolve_address(payload, current_customer)
        logger.info(
            "[Delivery estimate debug] event=address_resolved neighborhood=%s city=%s "
            "state=%s latitude=%s longitude=%s",
            getattr(address, "neighborhood", None),
            getattr(address, "city", None),
            getattr(address, "state", None),
            getattr(address, "latitude", None),
            getattr(address, "longitude", None),
        )

        zones = self.delivery_zone_repository.list_active_by_branch(
            restaurant.id,
            branch.id,
        )
        matching_zone = self.delivery_zone_repository.get_active_by_neighborhood(
            restaurant_id=restaurant.id,
            branch_id=branch.id,
            neighborhood=address.neighborhood,
        )
        if zones and matching_zone is None:
            result = self._not_serviceable(
                "outside_delivery_area",
                "Endereco fora da area de entrega.",
            )
            logger.info(
                "[Delivery estimate debug] event=delivery_zone neighborhood=%s "
                "zone_found=false delivery_fee=%s serviceable=%s reason=%s",
                getattr(address, "neighborhood", None),
                None,
                str(result.serviceable).lower(),
                result.reason,
            )
            latitude = self._optional_float(getattr(address, "latitude", None))
            longitude = self._optional_float(getattr(address, "longitude", None))
            if latitude is not None and longitude is not None:
                policy_key = (
                    self._cache_key(
                        restaurant_slug,
                        branch.id,
                        Coordinates(latitude=latitude, longitude=longitude),
                        None,
                        None,
                    )
                    + ":outside"
                )
                cached = self.cache.get(policy_key)
                if cached is not None:
                    self._log_final_result(cached)
                    return cached
                self.cache.set(policy_key, result)
            self._log_final_result(result)
            return result

        delivery_fee = money_to_float(
            matching_zone.delivery_fee
            if matching_zone is not None
            else getattr(restaurant_settings, "default_delivery_fee", Decimal("0"))
        )
        logger.info(
            "[Delivery estimate debug] event=delivery_zone neighborhood=%s "
            "zone_found=%s delivery_fee=%s serviceable=true reason=%s",
            getattr(address, "neighborhood", None),
            str(matching_zone is not None).lower(),
            delivery_fee,
            None,
        )
        prep_time_min, prep_time_max, prep_time_source, prep_time_weekday = self._resolve_prep_time(
            branch.id
        )
        logger.info(
            "[Delivery estimate debug] event=prep_time_resolved weekday=%s "
            "prep_time_min=%s prep_time_max=%s source=%s",
            prep_time_weekday,
            prep_time_min,
            prep_time_max,
            prep_time_source,
        )

        cache_key = None
        destination = None
        try:
            origin = self._resolve_coordinates(branch, branch=True)
            destination = self._resolve_coordinates(address, branch=False)
            cache_key = self._cache_key(
                restaurant_slug,
                branch.id,
                destination,
                prep_time_min,
                prep_time_max,
            )
            cached = self.cache.get(cache_key)
            if cached is not None:
                logger.info(
                    "[Delivery estimate] provider=%s cache_hit=true serviceable=%s",
                    cached.provider,
                    str(cached.serviceable).lower(),
                )
                logger.info(
                    "[Delivery estimate debug] event=google_maps_result provider=%s "
                    "distance_km=%s travel_time_min=%s fallback=%s",
                    cached.provider,
                    cached.distance_km,
                    cached.travel_time_min,
                    str(cached.fallback).lower(),
                )
                self._log_final_result(cached)
                return cached

            route = self.maps_client.compute_route(origin, destination)
            travel_time_min = route.travel_time_min
            eta_min = prep_time_min + travel_time_min
            eta_max = prep_time_max + travel_time_min
            result = DeliveryEstimateResult(
                serviceable=True,
                reason=None,
                message=None,
                distance_km=route.distance_km,
                travel_time_min=travel_time_min,
                prep_time_min=prep_time_min,
                prep_time_max=prep_time_max,
                eta_min=eta_min,
                eta_max=eta_max,
                delivery_fee=delivery_fee,
                provider=settings.DELIVERY_ESTIMATE_PROVIDER,
                fallback=False,
                latitude=destination.latitude,
                longitude=destination.longitude,
            )
            logger.info(
                "[Delivery estimate debug] event=google_maps_result provider=%s "
                "distance_km=%s travel_time_min=%s fallback=%s",
                result.provider,
                result.distance_km,
                result.travel_time_min,
                str(result.fallback).lower(),
            )
            self.cache.set(cache_key, result)
        except GoogleMapsLocationNotFoundError:
            result = self._not_serviceable(
                "route_not_found",
                "Nao foi possivel calcular uma rota para este endereco.",
                provider="google_routes",
            )
            logger.info(
                "[Delivery estimate debug] event=google_maps_result provider=%s "
                "distance_km=%s travel_time_min=%s fallback=%s",
                result.provider,
                result.distance_km,
                result.travel_time_min,
                str(result.fallback).lower(),
            )
            if cache_key is not None:
                self.cache.set(cache_key, result)
        except GoogleMapsUnavailableError:
            result = DeliveryEstimateResult(
                serviceable=True,
                reason="provider_unavailable",
                message="Estimativa baseada na configuracao do restaurante.",
                distance_km=None,
                travel_time_min=None,
                prep_time_min=prep_time_min,
                prep_time_max=prep_time_max,
                eta_min=prep_time_min,
                eta_max=prep_time_max,
                delivery_fee=delivery_fee,
                provider="configured_fallback",
                fallback=True,
                latitude=(
                    destination.latitude
                    if destination is not None
                    else self._optional_float(getattr(address, "latitude", None))
                ),
                longitude=(
                    destination.longitude
                    if destination is not None
                    else self._optional_float(getattr(address, "longitude", None))
                ),
            )
            logger.info(
                "[Delivery estimate debug] event=google_maps_result provider=%s "
                "distance_km=%s travel_time_min=%s fallback=%s",
                result.provider,
                result.distance_km,
                result.travel_time_min,
                str(result.fallback).lower(),
            )

        logger.info(
            "[Delivery estimate] provider=%s cache_hit=false serviceable=%s fallback=%s",
            result.provider,
            str(result.serviceable).lower(),
            str(result.fallback).lower(),
        )
        self._log_final_result(result)
        return result

    def _resolve_branch(self, restaurant_id, branch_id):
        if branch_id is not None:
            branch = self.branch_repository.get_active_by_id_and_restaurant(
                branch_id,
                restaurant_id,
            )
            if branch is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Filial invalida para este restaurante",
                )
            return branch
        branches = self.branch_repository.list_active_by_restaurant(restaurant_id)
        main_branch = next(
            (branch for branch in branches if branch.is_main is True),
            None,
        )
        if main_branch is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Restaurante sem filial principal ativa",
            )
        return main_branch

    def _resolve_address(
        self,
        payload: DeliveryEstimateRequest,
        current_customer: Customer | None,
    ):
        if payload.address_id is not None:
            if current_customer is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Autenticacao obrigatoria para usar address_id",
                )
            address = self.customer_repository.get_address(
                current_customer.id,
                payload.address_id,
            )
            if address is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Endereco nao encontrado",
                )
            return address
        return payload.address

    def _resolve_coordinates(self, location, *, branch: bool) -> Coordinates:
        latitude = self._optional_float(getattr(location, "latitude", None))
        longitude = self._optional_float(getattr(location, "longitude", None))
        if (latitude is None) != (longitude is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Latitude e longitude devem ser informadas juntas",
            )
        if latitude is not None and longitude is not None:
            return Coordinates(latitude=latitude, longitude=longitude)
        address = self._format_address(location, branch=branch)
        if not address:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Local sem endereco ou coordenadas suficientes",
            )
        return self.maps_client.geocode(address)

    def _resolve_prep_time(self, branch_id) -> tuple[int, int, str, int]:
        now = datetime.now(DELIVERY_TIMEZONE)
        current_time = now.timetz().replace(tzinfo=None)
        weekday = now.weekday()
        business_hours = self.branch_repository.list_business_hours_by_weekday(
            branch_id,
            weekday,
        )
        business_hour = self._select_business_hour_for_prep_time(
            business_hours,
            current_time,
        )
        prep_time_min, prep_time_max = self._prep_time_pair_from(business_hour)
        if prep_time_min is not None and prep_time_max is not None:
            return prep_time_min, max(prep_time_min, prep_time_max), "branch_business_hours", weekday

        return 30, 60, "default_30_60", weekday

    @classmethod
    def _select_business_hour_for_prep_time(
        cls,
        business_hours,
        current_time: datetime_time,
    ):
        current_period = next(
            (
                item
                for item in business_hours
                if cls._time_is_between(
                    current_time,
                    getattr(item, "opens_at", None),
                    getattr(item, "closes_at", None),
                )
            ),
            None,
        )
        if current_period is not None:
            return current_period
        return next(iter(business_hours), None)

    @staticmethod
    def _prep_time_pair_from(source) -> tuple[int | None, int | None]:
        prep_time_min = getattr(source, "prep_time_min", None)
        prep_time_max = getattr(source, "prep_time_max", None)
        if prep_time_min is None or prep_time_max is None:
            return None, None
        return int(prep_time_min), int(prep_time_max)

    @staticmethod
    def _time_is_between(
        current_time: datetime_time,
        opens_at: datetime_time | None,
        closes_at: datetime_time | None,
    ) -> bool:
        if opens_at is None or closes_at is None:
            return False
        if opens_at <= closes_at:
            return opens_at <= current_time <= closes_at
        return current_time >= opens_at or current_time <= closes_at

    @staticmethod
    def _format_address(location, *, branch: bool) -> str:
        if branch:
            parts = (
                getattr(location, "address", None),
                getattr(location, "neighborhood", None),
                getattr(location, "city", None),
                getattr(location, "state", None),
                getattr(location, "zipcode", None),
                "Brasil",
            )
        else:
            parts = (
                getattr(location, "street", None),
                getattr(location, "number", None),
                getattr(location, "neighborhood", None),
                getattr(location, "city", None),
                getattr(location, "state", None),
                getattr(location, "zipcode", None),
                "Brasil",
            )
        return ", ".join(str(part).strip() for part in parts if part)

    @staticmethod
    def _optional_float(value) -> float | None:
        return float(value) if value is not None else None

    @staticmethod
    def _not_serviceable(
        reason: str,
        message: str,
        provider: str = "local_policy",
    ) -> DeliveryEstimateResult:
        return DeliveryEstimateResult(
            serviceable=False,
            reason=reason,
            message=message,
            distance_km=None,
            travel_time_min=None,
            prep_time_min=None,
            prep_time_max=None,
            eta_min=None,
            eta_max=None,
            delivery_fee=None,
            provider=provider,
            fallback=False,
            latitude=None,
            longitude=None,
        )

    @staticmethod
    def _log_final_result(result: DeliveryEstimateResult) -> None:
        logger.info(
            "[Delivery estimate debug] event=final_result delivery_fee=%s "
            "prep_time_min=%s prep_time_max=%s travel_time_min=%s eta_min=%s "
            "eta_max=%s fallback=%s reason=%s",
            result.delivery_fee,
            result.prep_time_min,
            result.prep_time_max,
            result.travel_time_min,
            result.eta_min,
            result.eta_max,
            str(result.fallback).lower(),
            result.reason,
        )

    @staticmethod
    def _cache_key(
        restaurant_slug: str,
        branch_id,
        destination: Coordinates,
        prep_time_min: int | None,
        prep_time_max: int | None,
    ) -> str:
        ttl = max(1, settings.DELIVERY_ESTIMATE_CACHE_TTL_SECONDS)
        bucket = int(time() // ttl)
        return (
            f"delivery-estimate:v1:{restaurant_slug}:{branch_id}:"
            f"{destination.latitude:.4f}:{destination.longitude:.4f}:"
            f"{prep_time_min or 'none'}:{prep_time_max or 'none'}:{bucket}"
        )
