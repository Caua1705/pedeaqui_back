import logging
import math
from dataclasses import dataclass
from time import perf_counter

import httpx


logger = logging.getLogger("uvicorn.error")


class GoogleMapsError(Exception):
    pass


class GoogleMapsUnavailableError(GoogleMapsError):
    pass


class GoogleMapsLocationNotFoundError(GoogleMapsError):
    pass


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class RouteMetrics:
    distance_km: float
    travel_time_min: int


class GoogleMapsRoutesClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        routing_preference: str,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.routing_preference = routing_preference

    def geocode(self, address: str) -> Coordinates:
        self._ensure_configured()
        started_at = perf_counter()
        status_code = 0
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(
                    "https://maps.googleapis.com/maps/api/geocode/json",
                    params={
                        "address": address,
                        "components": "country:BR",
                        "key": self.api_key,
                    },
                )
                status_code = response.status_code
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GoogleMapsUnavailableError("Google Geocoding indisponivel") from exc
        finally:
            logger.info(
                "[Delivery Google] operation=geocode latency_ms=%.2f status=%s",
                (perf_counter() - started_at) * 1000,
                status_code,
            )

        if payload.get("status") == "ZERO_RESULTS":
            raise GoogleMapsLocationNotFoundError("Endereco nao localizado")
        if payload.get("status") != "OK" or not payload.get("results"):
            raise GoogleMapsUnavailableError("Falha no Google Geocoding")
        location = payload["results"][0]["geometry"]["location"]
        return Coordinates(
            latitude=float(location["lat"]),
            longitude=float(location["lng"]),
        )

    def compute_route(
        self,
        origin: Coordinates,
        destination: Coordinates,
    ) -> RouteMetrics:
        self._ensure_configured()
        started_at = perf_counter()
        status_code = 0
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{self.base_url}/directions/v2:computeRoutes",
                    headers={
                        "Content-Type": "application/json",
                        "X-Goog-Api-Key": self.api_key,
                        "X-Goog-FieldMask": "routes.distanceMeters,routes.duration",
                    },
                    json={
                        "origin": self._waypoint(origin),
                        "destination": self._waypoint(destination),
                        "travelMode": "DRIVE",
                        "routingPreference": self.routing_preference,
                        "computeAlternativeRoutes": False,
                        "languageCode": "pt-BR",
                        "units": "METRIC",
                    },
                )
                status_code = response.status_code
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GoogleMapsUnavailableError("Google Routes indisponivel") from exc
        finally:
            logger.info(
                "[Delivery Google] operation=compute_route latency_ms=%.2f status=%s",
                (perf_counter() - started_at) * 1000,
                status_code,
            )

        routes = payload.get("routes") or []
        if not routes:
            raise GoogleMapsLocationNotFoundError("Rota nao encontrada")
        route = routes[0]
        duration_seconds = float(str(route["duration"]).removesuffix("s"))
        return RouteMetrics(
            distance_km=round(float(route["distanceMeters"]) / 1000, 2),
            travel_time_min=max(1, math.ceil(duration_seconds / 60)),
        )

    def _ensure_configured(self) -> None:
        if not self.api_key:
            raise GoogleMapsUnavailableError("Google Maps nao configurado")

    @staticmethod
    def _waypoint(coordinates: Coordinates) -> dict:
        return {
            "location": {
                "latLng": {
                    "latitude": coordinates.latitude,
                    "longitude": coordinates.longitude,
                }
            }
        }
