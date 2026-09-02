import hashlib
import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
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
from src.models.delivery_estimate_model import DeliveryEstimate
from src.repositories.branch_repository import BranchRepository
from src.repositories.customer_repository import CustomerRepository
from src.repositories.delivery_estimate_repository import DeliveryEstimateRepository
from src.repositories.menu_repository import MenuRepository
from src.schemas.delivery_schema import (
    DeliveryAddressInput,
    DeliveryEstimateRequest,
    DeliveryEstimateResponse,
)
from src.services.branch_hours_service import BranchHoursService
from src.services.branch_operation import BranchOperation, resolve_branch_operation
from src.services.restaurant_service import RestaurantService
from src.utils.money import ZERO, money_to_float, quantize_money, to_decimal
from src.utils.security import generate_tracking_token, utcnow


logger = logging.getLogger("uvicorn.error")
DELIVERY_TIMEZONE = ZoneInfo("America/Fortaleza")

# Valor de `provider` quando a taxa veio do `default_delivery_fee`
# resolvido (o da filial, ou o do restaurante que ela herda) em vez da
# regra por km da filial. Vai gravado em `orders.delivery_estimate_provider`, e e por ele que
# se separa depois o pedido precificado por rota do precificado no modo de
# contingencia.
FALLBACK_FEE_PROVIDER = "configured_fallback"


def build_address_fingerprint(payload: DeliveryEstimateRequest) -> str:
    """Identidade do endereco estimado.

    Serve para amarrar uma estimativa guardada ao endereco que ela mediu:
    sem isso, o cliente pediria a estimativa para o endereco a 500m e
    fecharia o pedido para o de 15km pagando a taxa do primeiro.

    Endereco de conta e identificado pelo id (o conteudo dele pode ser
    editado, e ai a estimativa antiga deixa de valer — o id continua o
    mesmo, mas a checagem de expiracao limita a janela a 15 minutos).
    Endereco avulso e identificado pelo conteudo que afeta a rota;
    complemento e ponto de referencia ficam de fora porque nao mudam
    distancia nenhuma.
    """
    if payload.address_id is not None:
        return f"address_id:{payload.address_id}"

    address = payload.address
    parts = (
        _normalized(address.street),
        _normalized(address.number),
        _normalized(address.neighborhood),
        _normalized(address.city),
        _normalized(address.state),
        _digits(address.zipcode),
        _coordinate(address.latitude),
        _coordinate(address.longitude),
    )
    canonical = "|".join(parts)
    return "address:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _mensagem_da_pausa(operation: BranchOperation) -> str:
    """"Sem entrega agora" com PRAZO e, quando houver, com motivo.

    A frase existe inteira por uma razao de negocio: pausa sem prazo faz o
    cliente fechar o app, e pausa com prazo faz ele voltar as 20h30. O motivo
    e opcional porque "chuva forte" ajuda e a ausencia dele nao atrapalha.

    O horario sai no fuso da operacao, e nao em UTC: "voltamos as 23:30" para
    uma pausa que acaba as 20h30 e pior que nao dizer nada.
    """
    partes = ["A entrega esta pausada no momento."]
    if operation.delivery_pause_reason:
        partes.append(f"Motivo: {operation.delivery_pause_reason}.")
    if operation.delivery_paused_until is not None:
        volta = operation.delivery_paused_until.astimezone(DELIVERY_TIMEZONE)
        partes.append(f"Voltamos a entregar as {volta.strftime('%H:%M')}.")
    return " ".join(partes)


def _normalized(value: str | None) -> str:
    return (value or "").strip().casefold()


def _digits(value: str | None) -> str:
    return "".join(character for character in (value or "") if character.isdigit())


def _coordinate(value) -> str:
    # Arredondado para 5 casas (~1 metro): ruido do GPS do celular nao pode
    # invalidar a estimativa que o proprio cliente acabou de pedir.
    return "" if value is None else f"{float(value):.5f}"


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
        self.db = db
        self.restaurant_service = RestaurantService(db)
        self.branch_repository = BranchRepository(db)
        self.branch_hours_service = BranchHoursService(db)
        self.customer_repository = CustomerRepository(db)
        self.menu_repository = MenuRepository(db)
        self.delivery_estimate_repository = DeliveryEstimateRepository(db)
        self.maps_client = maps_client or GoogleMapsRoutesClient(
            api_key=settings.GOOGLE_MAPS_ROUTES_API_KEY,
            base_url=settings.GOOGLE_MAPS_ROUTES_BASE_URL,
            timeout_seconds=settings.GOOGLE_MAPS_TIMEOUT_SECONDS,
            routing_preference=settings.GOOGLE_MAPS_ROUTING_PREFERENCE,
        )
        self.cache = cache or DeliveryEstimateCache()
        # O RELOGIO, INJETAVEL — mesmo desenho de `CouponService.clock`.
        #
        # Sem ele, `_resolve_prep_time` lia `datetime.now()` direto e todo
        # teste de estimativa passava a depender do minuto em que rodasse. Nao
        # e hipotese: a faixa "dia inteiro" que os testes usam vai de 00:00 a
        # 23:59, e `_period_covers_same_day` compara `current_time <=
        # closes_at` — entre 23:59:01 e 23:59:59 a filial estava FECHADA e as
        # dezenas de testes que dependem dela falhavam. Um minuto por dia.
        #
        # Fuso do BRASIL e nao UTC: quem decide se a loja esta aberta e o
        # relogio da rua, e `weekday()` de um instante UTC vira o dia errado
        # nas tres primeiras horas da madrugada.
        self.clock = lambda: datetime.now(DELIVERY_TIMEZONE)

    def estimate_and_store(
        self,
        restaurant_slug: str,
        payload: DeliveryEstimateRequest,
        current_customer: Customer | None,
    ) -> tuple[DeliveryEstimateResult, DeliveryEstimate | None]:
        """Calcula a estimativa e guarda o resultado para reaproveitamento.

        Separado de `estimate` porque ESTE metodo escreve e commita. O
        `estimate` cru continua sem efeito colateral, que e o que permite
        chama-lo de dentro da transacao do pedido.

        O que e guardado alimenta a criacao do pedido minutos depois, sem
        refazer geocode e rota (as duas chamadas pagas do Google).
        """
        result = self.estimate(restaurant_slug, payload, current_customer)
        if not result.serviceable:
            # Nao ha o que reaproveitar: "fora da area" e "loja fechada"
            # precisam ser reavaliados no momento do pedido.
            return result, None

        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        branch = self._resolve_branch(restaurant.id, payload.branch_id)
        now = utcnow()
        stored = DeliveryEstimate(
            token=generate_tracking_token(),
            restaurant_id=restaurant.id,
            branch_id=branch.id,
            customer_id=current_customer.id if current_customer else None,
            address_fingerprint=build_address_fingerprint(payload),
            distance_km=to_decimal(result.distance_km) if result.distance_km is not None else None,
            travel_time_min=result.travel_time_min,
            prep_time_min=result.prep_time_min,
            prep_time_max=result.prep_time_max,
            eta_min=result.eta_min,
            eta_max=result.eta_max,
            delivery_fee=quantize_money(to_decimal(result.delivery_fee)),
            latitude=to_decimal(result.latitude) if result.latitude is not None else None,
            longitude=to_decimal(result.longitude) if result.longitude is not None else None,
            provider=result.provider,
            expires_at=now + timedelta(seconds=settings.DELIVERY_ESTIMATE_REUSE_TTL_SECONDS),
        )
        try:
            self.delivery_estimate_repository.create(stored)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return result, stored

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
        # A filial e resolvida ANTES das duas recusas abaixo, e essa ordem
        # mudou na revisao 20260818_0025. Antes `accepts_delivery` era do
        # restaurante e dava para responder sem saber qual filial era; agora
        # ele e da filial, e um `branch_id` invalido passa a responder 400
        # antes de qualquer veredito de entrega — que e a ordem certa: nao se
        # responde "esta filial nao entrega" sobre uma filial que nao existe.
        branch = self._resolve_branch(restaurant.id, payload.branch_id)
        operation = resolve_branch_operation(branch, restaurant_settings)
        if not operation.accepts_delivery:
            result = self._not_serviceable("delivery_disabled", "Esta filial nao aceita entrega.")
            self._log_final_result(result)
            return result
        if not operation.accepts_delivery_now:
            # `reason` PROPRIO, e nao o `delivery_disabled` acima: as duas
            # situacoes pedem telas diferentes. "Esta loja nao entrega" e
            # definitivo e manda o cliente escolher outra filial; "voltamos as
            # 20h30" e um convite a esperar, e o app so consegue mostrar isso
            # se souber distinguir. Foi a licao contraria a de `branch_closed`,
            # onde juntar os dois casos foi o certo — la o cliente faz a mesma
            # coisa nos dois; aqui, nao.
            result = self._not_serviceable(
                "delivery_paused",
                _mensagem_da_pausa(operation),
            )
            self._log_final_result(result)
            return result
        if not operation.is_open:
            # Mesmo `reason` do fechado-por-horario, de proposito: para o
            # cliente as duas coisas sao a mesma ("esta loja nao esta
            # atendendo agora"), e um codigo novo obrigaria todo front a
            # tratar um caso que se resolve com a mesma tela. Quem precisa
            # distinguir e o painel, e la o campo e `is_open`.
            result = self._not_serviceable("branch_closed", "A loja esta fechada no momento.")
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
        # Bairro, cidade e estado bastam para depurar area de entrega — sao o
        # recorte que a regra usa. A COORDENADA nao entra: ela e mais precisa
        # que rua e numero, e quem tivesse o log do container reconstruiria a
        # casa de todo mundo que pediu entrega. Saber SE ela existe e o que
        # separa "endereco sem geocode" de "rota falhou", que era a pergunta
        # que a coordenada respondia aqui. Se um dia o valor for mesmo
        # necessario, `logger.debug` (que nao roda em producao) — nunca info.
        has_coordinates = (
            getattr(address, "latitude", None) is not None
            and getattr(address, "longitude", None) is not None
        )
        logger.info(
            "[Delivery estimate debug] event=address_resolved neighborhood=%s city=%s "
            "state=%s has_coordinates=%s",
            getattr(address, "neighborhood", None),
            getattr(address, "city", None),
            getattr(address, "state", None),
            str(has_coordinates).lower(),
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
        if prep_time_min is None or prep_time_max is None:
            # Duas causas diferentes, dois motivos diferentes: "fechado agora"
            # e operacao normal e o cliente entende; "sem tempo de preparo
            # cadastrado" e configuracao faltando e alguem precisa arrumar.
            if prep_time_source == "branch_closed":
                result = self._not_serviceable(
                    "branch_closed",
                    "A loja esta fechada neste horario.",
                )
            else:
                result = self._not_serviceable(
                    "prep_time_unavailable",
                    "Tempo de preparo nao configurado para o horario atual.",
                )
            self._log_final_result(result)
            return result

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
                branch,
                self._bands_fingerprint(branch.id),
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
                if cached.distance_km is not None and cached.travel_time_min is not None:
                    _, raw_delivery_fee = self._calculate_delivery_fee(
                        branch,
                        cached.distance_km,
                    )
                    self._log_delivery_fee_algorithm(
                        branch,
                        cached.distance_km,
                        cached.travel_time_min,
                        raw_delivery_fee,
                        to_decimal(cached.delivery_fee)
                        if cached.delivery_fee is not None
                        else None,
                        cached.prep_time_min,
                        cached.prep_time_max,
                        cached.eta_min,
                        cached.eta_max,
                        cached.fallback,
                    )
                self._log_final_result(cached)
                return cached

            route = self.maps_client.compute_route(origin, destination)
            delivery_fee, raw_delivery_fee = self._calculate_delivery_fee(
                branch,
                route.distance_km,
            )
            fee_is_fallback = False
            if delivery_fee is None or raw_delivery_fee is None:
                # A filial nao tem base/por-km cadastrados. A rota existe, so
                # falta a regra de preco — e e exatamente para isso que serve
                # o `default_delivery_fee` do restaurante. Com ele
                # configurado, o pedido segue com a taxa fixa e continua
                # passando pela conferencia de distancia maxima logo abaixo,
                # porque aqui a distancia E conhecida. Sem ele, vale o
                # comportamento antigo: nao atendido.
                fallback_fee = self._configured_fallback_fee(operation)
                if fallback_fee is None:
                    result = self._not_serviceable(
                        "delivery_fee_config_unavailable",
                        "Configuracao de taxa de entrega nao encontrada para a filial.",
                        provider=settings.DELIVERY_ESTIMATE_PROVIDER,
                    )
                    result.distance_km = route.distance_km
                    result.travel_time_min = route.travel_time_min
                    result.prep_time_min = prep_time_min
                    result.prep_time_max = prep_time_max
                    result.eta_min = prep_time_min + route.travel_time_min
                    result.eta_max = prep_time_max + route.travel_time_min
                    result.latitude = destination.latitude
                    result.longitude = destination.longitude
                    self._log_delivery_fee_algorithm(
                        branch,
                        route.distance_km,
                        route.travel_time_min,
                        raw_delivery_fee,
                        result.delivery_fee,
                        prep_time_min,
                        prep_time_max,
                        result.eta_min,
                        result.eta_max,
                        result.fallback,
                    )
                    self._log_final_result(result)
                    return result
                delivery_fee = fallback_fee
                raw_delivery_fee = fallback_fee
                fee_is_fallback = True
            if self._is_outside_max_distance(branch, route.distance_km):
                result = self._not_serviceable(
                    "outside_delivery_area",
                    "Endereco fora da area de entrega.",
                    provider=settings.DELIVERY_ESTIMATE_PROVIDER,
                )
                result.distance_km = route.distance_km
                result.travel_time_min = route.travel_time_min
                result.prep_time_min = prep_time_min
                result.prep_time_max = prep_time_max
                result.eta_min = prep_time_min + route.travel_time_min
                result.eta_max = prep_time_max + route.travel_time_min
                result.latitude = destination.latitude
                result.longitude = destination.longitude
                self._log_delivery_fee_algorithm(
                    branch,
                    route.distance_km,
                    route.travel_time_min,
                    raw_delivery_fee,
                    result.delivery_fee,
                    prep_time_min,
                    prep_time_max,
                    result.eta_min,
                    result.eta_max,
                    result.fallback,
                )
                self.cache.set(cache_key, result)
                self._log_final_result(result)
                return result
            travel_time_min = route.travel_time_min
            eta_min, eta_max = self._eta_from_bands(
                branch.id,
                route.distance_km,
                prep_time_min,
                prep_time_max,
                travel_time_min,
            )
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
                delivery_fee=money_to_float(delivery_fee),
                provider=(
                    FALLBACK_FEE_PROVIDER
                    if fee_is_fallback
                    else settings.DELIVERY_ESTIMATE_PROVIDER
                ),
                # `fallback=True` significa "esta taxa nao saiu da regra da
                # filial". O pedido grava o provider em
                # `orders.delivery_estimate_provider`, que e como o lojista
                # separa depois o que foi precificado por rota do que foi
                # precificado pelo padrao.
                fallback=fee_is_fallback,
                latitude=destination.latitude,
                longitude=destination.longitude,
            )
            self._log_delivery_fee_algorithm(
                branch,
                route.distance_km,
                travel_time_min,
                raw_delivery_fee,
                delivery_fee,
                prep_time_min,
                prep_time_max,
                eta_min,
                eta_max,
                result.fallback,
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
            # O Google caiu. Sem rota nao ha distancia, e sem distancia a
            # regra por km da filial nao tem como ser aplicada.
            #
            # Antes daqui sair sempre `serviceable=False`, o provider ja se
            # chamava "configured_fallback" — o nome estava certo e o
            # fallback e que nao existia: uma indisponibilidade do Google
            # derrubava TODO pedido de entrega da plataforma. Agora
            # o `default_delivery_fee` resolvido e esse fallback.
            #
            # O que se perde ao aceitar: a distancia e desconhecida, entao
            # `delivery_max_distance_km` NAO e conferida neste caminho. Um
            # endereco fora da area passa pela estimativa. Isso e aceito
            # porque o pedido nasce em `pending` e ainda precisa ser aceito
            # pelo lojista, que ve `delivery_estimate_provider =
            # configured_fallback` no pedido e pode recusar. A alternativa —
            # recusar tudo — ja e o pior resultado possivel para os dois
            # lados durante uma queda do Google.
            fallback_fee = self._configured_fallback_fee(operation)
            result = DeliveryEstimateResult(
                serviceable=fallback_fee is not None,
                reason=None if fallback_fee is not None else "route_unavailable",
                message=(
                    None
                    if fallback_fee is not None
                    else "Nao foi possivel calcular a rota para estimar a taxa de entrega."
                ),
                distance_km=None,
                travel_time_min=None,
                prep_time_min=prep_time_min,
                prep_time_max=prep_time_max,
                # Sem tempo de deslocamento no ETA: e o unico numero honesto
                # possivel sem rota. Fica subestimado, e o cliente ve que a
                # estimativa veio do modo de contingencia pelo provider.
                eta_min=prep_time_min,
                eta_max=prep_time_max,
                delivery_fee=(
                    money_to_float(fallback_fee) if fallback_fee is not None else None
                ),
                provider=FALLBACK_FEE_PROVIDER,
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

    def resolve_destination_address(
        self,
        payload: DeliveryEstimateRequest,
        current_customer: Customer | None,
    ) -> DeliveryAddressInput:
        """O endereco do cliente com a coordenada JA resolvida.

        Existe para quem precisa estimar o MESMO endereco contra varias
        filiais (a tela de escolha de filial). `estimate` geocodifica quando o
        endereco chega sem coordenada; chamando-o N vezes com o endereco cru,
        seriam N geocodes pagos do mesmo lugar. Resolvendo uma vez aqui e
        passando o resultado adiante, aquele caminho nunca e tomado.

        O texto do endereco vai junto, e nao so a coordenada, porque e ele que
        aparece nos logs de diagnostico de `estimate` — devolver so o par de
        numeros faria as N estimativas seguintes registrarem bairro e cidade
        vazios.
        """
        address = self._resolve_address(payload, current_customer)
        coordinates = self._resolve_coordinates(address, branch=False)
        return DeliveryAddressInput(
            street=self._address_text(address, "street") or "-",
            number=self._address_text(address, "number") or "-",
            neighborhood=self._address_text(address, "neighborhood") or "-",
            city=self._address_text(address, "city"),
            state=self._address_text(address, "state"),
            zipcode=self._address_text(address, "zipcode"),
            latitude=to_decimal(coordinates.latitude),
            longitude=to_decimal(coordinates.longitude),
        )

    @staticmethod
    def _address_text(address, field: str) -> str | None:
        value = getattr(address, field, None)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _resolve_branch(self, restaurant_id, branch_id):
        if branch_id is not None:
            branch = self.branch_repository.get_active_by_id_and_restaurant(
                branch_id,
                restaurant_id,
            )
            if branch is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Filial inválida para este restaurante",
                )
            return branch

        # `get_default_branch` e a MESMA escolha que o /restaurants/{slug}/info
        # faz — ver o docstring dela para as duas regras que existiam antes.
        # O que muda aqui: restaurante com filial ativa mas sem `is_main`
        # marcado deixa de receber 400 e passa a ser atendido pela primeira
        # filial ativa, como ja acontecia no /info. O 400 fica para o caso que
        # ele sempre quis descrever: restaurante sem filial ativa nenhuma.
        default_branch = self.branch_repository.get_default_branch(restaurant_id)
        if default_branch is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Restaurante sem filial principal ativa",
            )
        return default_branch

    def _resolve_address(
        self,
        payload: DeliveryEstimateRequest,
        current_customer: Customer | None,
    ):
        if payload.address_id is not None:
            if current_customer is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Autenticação obrigatória para usar address_id",
                )
            address = self.customer_repository.get_address(
                current_customer.id,
                payload.address_id,
            )
            if address is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Endereço não encontrado",
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
                detail="Local sem endereço ou coordenadas suficientes",
            )
        return self.maps_client.geocode(address)

    def _eta_from_bands(
        self,
        branch_id,
        distance_km: float,
        prep_time_min: int,
        prep_time_max: int,
        travel_time_min: int,
    ) -> tuple[int, int]:
        """O prazo prometido ao cliente: preparo + deslocamento.

        Quem responde pelo DESLOCAMENTO e a faixa da filial, quando existe
        uma que alcance esta distancia. O tempo do Google e tempo de DIRIGIR:
        ele nao inclui ensacar o pedido, a segunda entrega da mesma corrida,
        estacionar e subir escada — e e por isso que o prazo saia curto
        justamente no bairro longe, que e onde o cliente ja desconfia.

        Sem faixa que alcance (filial sem faixas, ou endereco alem do ultimo
        teto), vale o tempo do Google, que e o comportamento anterior a
        revisao 20260821_0030. Nao e degradacao: e a resposta certa para quem
        nao configurou nada.

        O PREPARO continua somando nos dois caminhos, e isso e o que mantem o
        botao de "+10 minutos" do almoco (`PATCH /branches/{id}/prep-time`)
        mexendo no prazo de quem pede entrega. Uma faixa que substituisse o
        ETA inteiro desligaria aquele botao para entrega e o deixaria valendo
        so para retirada — bem no horario em que ele existe para ser usado.
        """
        band = self._band_for(branch_id, distance_km)
        if band is None:
            return prep_time_min + travel_time_min, prep_time_max + travel_time_min
        return (
            prep_time_min + int(band.delivery_time_min),
            prep_time_max + int(band.delivery_time_max),
        )

    def _band_for(self, branch_id, distance_km: float):
        """A primeira faixa cujo teto alcanca esta distancia, ou None.

        As faixas sao TETOS em ordem crescente, entao a primeira que couber e
        a certa e nao ha buraco possivel entre elas — ver o docstring de
        `BranchDeliveryTimeBand`.
        """
        distancia = to_decimal(distance_km)
        for band in self.branch_repository.list_delivery_time_bands(branch_id):
            if distancia <= to_decimal(band.max_distance_km):
                return band
        return None

    def _bands_fingerprint(self, branch_id) -> str:
        bands = self.branch_repository.list_delivery_time_bands(branch_id)
        if not bands:
            return "none"
        partes = [
            f"{band.max_distance_km}-{band.delivery_time_min}-{band.delivery_time_max}"
            for band in bands
        ]
        return hashlib.sha256("|".join(partes).encode("utf-8")).hexdigest()[:12]

    def _resolve_prep_time(self, branch_id) -> tuple[int | None, int | None, str, int]:
        now = self.clock()
        weekday = now.weekday()
        # A escolha da faixa mudou de lugar: agora e BranchHoursService quem
        # decide, e ela devolve None quando o momento atual nao cai em faixa
        # nenhuma. Antes caia na primeira faixa do dia, o que fazia a filial
        # "atender" as 3h da manha com o prazo do almoco.
        business_hour = self.branch_hours_service.find_current_period(branch_id, now)
        if business_hour is None:
            return None, None, "branch_closed", weekday

        prep_time_min, prep_time_max = self._prep_time_pair_from(business_hour)
        if prep_time_min is not None and prep_time_max is not None:
            return prep_time_min, max(prep_time_min, prep_time_max), "branch_business_hours", weekday

        return None, None, "branch_business_hours_missing", weekday

    @staticmethod
    def _calculate_delivery_fee(branch, distance_km: float) -> tuple[Decimal | None, Decimal | None]:
        base_fee_config = getattr(branch, "delivery_base_fee", None)
        fee_per_km_config = getattr(branch, "delivery_fee_per_km", None)
        if base_fee_config is None or fee_per_km_config is None:
            return None, None

        base_fee = to_decimal(base_fee_config)
        fee_per_km = to_decimal(fee_per_km_config)
        raw_fee = base_fee + (to_decimal(distance_km) * fee_per_km)
        delivery_fee = raw_fee

        min_fee = getattr(branch, "delivery_min_fee", None)
        if min_fee is not None:
            delivery_fee = max(delivery_fee, to_decimal(min_fee))

        max_fee = getattr(branch, "delivery_max_fee", None)
        if max_fee is not None:
            delivery_fee = min(delivery_fee, to_decimal(max_fee))

        return quantize_money(delivery_fee), quantize_money(raw_fee)

    @staticmethod
    def _configured_fallback_fee(operation: BranchOperation) -> Decimal | None:
        """A taxa fixa a usar quando a regra da filial nao pode ser aplicada.

        `default_delivery_fee` so vale como fallback se for MAIOR QUE ZERO. A
        coluna e nullable e tem default 0, e a maioria das linhas em producao
        esta em 0 sem ninguem ter escolhido isso — tratar esse 0 como
        "entrega gratis na contingencia" faria uma queda do Google virar
        frete gratis para a plataforma inteira, sem que nenhum lojista
        tivesse pedido.

        O valor vem resolvido: e o da filial quando ela sobrescreveu, e o do
        restaurante quando nao. Desde a revisao 20260818_0025 a filial pode
        ter o proprio — a regra por km que este fallback substitui sempre foi
        da filial, e uma taxa de contingencia unica deixava a loja do outro
        lado da cidade com o mesmo numero da loja da esquina.
        """
        if operation.default_delivery_fee is None:
            return None
        fee = quantize_money(to_decimal(operation.default_delivery_fee))
        return fee if fee > ZERO else None

    @staticmethod
    def _is_outside_max_distance(branch, distance_km: float) -> bool:
        max_distance = getattr(branch, "delivery_max_distance_km", None)
        return max_distance is not None and to_decimal(distance_km) > to_decimal(max_distance)

    @staticmethod
    def _prep_time_pair_from(source) -> tuple[int | None, int | None]:
        prep_time_min = getattr(source, "prep_time_min", None)
        prep_time_max = getattr(source, "prep_time_max", None)
        if prep_time_min is None or prep_time_max is None:
            return None, None
        return int(prep_time_min), int(prep_time_max)

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
    def _log_delivery_fee_algorithm(
        branch,
        distance_km: float,
        travel_time_min: int,
        raw_fee: Decimal | None,
        final_delivery_fee: Decimal | None,
        prep_time_min: int | None,
        prep_time_max: int | None,
        eta_min: int | None,
        eta_max: int | None,
        fallback: bool,
    ) -> None:
        logger.info(
            "[Delivery estimate debug] event=delivery_fee_algorithm distance_km=%s "
            "travel_time_min=%s delivery_base_fee=%s delivery_fee_per_km=%s "
            "delivery_min_fee=%s delivery_max_fee=%s delivery_max_distance_km=%s "
            "raw_fee=%s final_delivery_fee=%s delivery_fee_source=%s "
            "prep_time_min=%s prep_time_max=%s eta_min=%s eta_max=%s fallback=%s",
            distance_km,
            travel_time_min,
            getattr(branch, "delivery_base_fee", None),
            getattr(branch, "delivery_fee_per_km", None),
            getattr(branch, "delivery_min_fee", None),
            getattr(branch, "delivery_max_fee", None),
            getattr(branch, "delivery_max_distance_km", None),
            raw_fee,
            final_delivery_fee,
            "distance_algorithm",
            prep_time_min,
            prep_time_max,
            eta_min,
            eta_max,
            str(fallback).lower(),
        )

    @staticmethod
    def _cache_key(
        restaurant_slug: str,
        branch_id,
        destination: Coordinates,
        prep_time_min: int | None,
        prep_time_max: int | None,
        branch,
        bands_fingerprint: str,
    ) -> str:
        """A chave carrega TUDO que muda o resultado guardado.

        As faixas de prazo entraram na chave junto com a feature, e nao
        depois: sem elas, editar a faixa no painel continuaria servindo o
        prazo antigo por ate 20 minutos (DELIVERY_ESTIMATE_CACHE_TTL_SECONDS)
        — e o lojista que acabou de corrigir um prazo desonesto veria a
        correcao nao surtir efeito, sem nada no log.
        """
        ttl = max(1, settings.DELIVERY_ESTIMATE_CACHE_TTL_SECONDS)
        bucket = int(time() // ttl)
        return (
            f"delivery-estimate:v1:{restaurant_slug}:{branch_id}:"
            f"{destination.latitude:.4f}:{destination.longitude:.4f}:"
            f"{prep_time_min or 'none'}:{prep_time_max or 'none'}:"
            f"{getattr(branch, 'delivery_base_fee', None) or 'none'}:"
            f"{getattr(branch, 'delivery_fee_per_km', None) or 'none'}:"
            f"{getattr(branch, 'delivery_min_fee', None) or 'none'}:"
            f"{getattr(branch, 'delivery_max_fee', None) or 'none'}:"
            f"{getattr(branch, 'delivery_max_distance_km', None) or 'none'}:"
            f"{bands_fingerprint}:"
            f"{bucket}"
        )
