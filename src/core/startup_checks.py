"""Validacao de configuracao executada no boot da aplicacao.

Roda no lifespan, e nao no import, para que importar `main` (testes,
ferramentas, scripts) nao dependa de um ambiente completo, mas subir a API
com configuracao incompleta falhe alto e na hora.
"""

import logging

from src.core.config import Settings


logger = logging.getLogger("uvicorn.error")


class StartupConfigurationError(RuntimeError):
    pass


def collect_configuration_errors(settings: Settings) -> list[str]:
    errors: list[str] = []

    if settings.DELIVERY_ESTIMATE_PROVIDER == "google_routes" and not settings.GOOGLE_MAPS_ROUTES_API_KEY.strip():
        errors.append(
            "GOOGLE_MAPS_ROUTES_API_KEY esta vazia com "
            "DELIVERY_ESTIMATE_PROVIDER=google_routes. Sem ela o cliente de "
            "rotas levanta GoogleMapsUnavailableError e TODO pedido de entrega "
            "e recusado com 'route_unavailable', sem erro visivel. Defina a "
            "chave ou mude DELIVERY_ESTIMATE_PROVIDER."
        )

    return errors


def collect_configuration_warnings(settings: Settings) -> list[str]:
    warnings: list[str] = []

    if settings.RATE_LIMIT_ENABLED and not settings.REDIS_URL:
        warnings.append(
            "REDIS_URL nao definida: o rate limiting usa contador em memoria, "
            "valido apenas para 1 worker/container. Com mais de um processo o "
            "limite efetivo vira N vezes o configurado."
        )

    if not settings.REDIS_URL:
        warnings.append(
            "REDIS_URL nao definida: o cache de estimativa de entrega fica so "
            "em memoria e e perdido a cada deploy, aumentando o custo de "
            "chamadas ao Google Maps."
        )

    if not settings.RATE_LIMIT_CLIENT_IP_HEADER.strip():
        warnings.append(
            "RATE_LIMIT_CLIENT_IP_HEADER vazio: atras de um proxy o rate "
            "limiting vai agrupar todos os clientes no IP do proxy."
        )

    return warnings


def validate_settings(settings: Settings) -> None:
    for warning in collect_configuration_warnings(settings):
        logger.warning("[Startup] %s", warning)

    errors = collect_configuration_errors(settings)
    if errors:
        joined = "\n".join(f"  - {error}" for error in errors)
        raise StartupConfigurationError(
            f"Configuracao invalida, a API nao vai subir:\n{joined}"
        )

    logger.info("[Startup] configuracao validada com sucesso")
