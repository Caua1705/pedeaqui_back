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

    if settings.PAYMENT_PROVIDER == "mercadopago" and not (settings.PAYMENT_CREDENTIALS_ENCRYPTION_KEY or "").strip():
        # A credencial em si e por restaurante e vive no banco (ver
        # restaurant_payment_credentials); o que da para checar no boot,
        # sem consultar o banco, e se existe chave para decifra-la. Sem
        # isso TODA cobranca online responde 503, restaurante nenhum
        # cadastrado ou nao.
        errors.append(
            "PAYMENT_PROVIDER=mercadopago sem PAYMENT_CREDENTIALS_ENCRYPTION_KEY. "
            "Nenhuma credencial de restaurante pode ser decifrada e toda "
            "cobranca online responderia 503. Gere uma chave com "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "ou volte PAYMENT_PROVIDER para sandbox."
        )

    if settings.MERCADOPAGO_ENVIRONMENT not in ("test", "production"):
        errors.append(
            f"MERCADOPAGO_ENVIRONMENT='{settings.MERCADOPAGO_ENVIRONMENT}' invalido: "
            "so 'test' ou 'production' selecionam qual credencial cadastrada "
            "em restaurant_payment_credentials e usada."
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

    if not settings.ADMIN_AUTH_SECRET:
        warnings.append(
            "ADMIN_AUTH_SECRET nao definida: os tokens de lojista sao "
            "assinados com CUSTOMER_AUTH_SECRET. Funciona (o campo `purpose` "
            "separa os dois tipos de token), mas um segredo proprio evita que "
            "o comprometimento de um alcance o outro."
        )

    if settings.INTERNAL_API_KEY:
        warnings.append(
            "INTERNAL_API_KEY ainda definida no ambiente: desde a Fase 1 "
            "nenhuma rota a utiliza. Pode ser removida do .env."
        )

    if settings.PAYMENT_PROVIDER == "sandbox" and not (settings.PAYMENT_WEBHOOK_SECRET or "").strip():
        # Warning e nao erro: hoje ha restaurante sem nenhuma forma de
        # pagamento online, e derrubar o boot deles por causa de uma
        # variavel que nao usam seria pior. Quem tem pagamento online
        # descobre no primeiro webhook, que responde 503.
        warnings.append(
            "PAYMENT_WEBHOOK_SECRET nao definida: o webhook do sandbox "
            "responde 503 e nenhum pedido online sai de 'aguardando "
            "pagamento'. Obrigatoria se a filial oferece pagamento online."
        )

    if settings.PAYMENT_PROVIDER == "mercadopago" and not (settings.MERCADOPAGO_WEBHOOK_SECRET or "").strip():
        warnings.append(
            "MERCADOPAGO_WEBHOOK_SECRET nao definida: o webhook do Mercado "
            "Pago responde 503 e nenhum pedido online sai de 'aguardando "
            "pagamento'. Pegue a 'Assinatura secreta' no painel do "
            "restaurante, em Webhooks, depois de cadastrar a Notification URL."
        )

    if settings.is_production and settings.PAYMENT_PROVIDER == "sandbox":
        warnings.append(
            "PAYMENT_PROVIDER=sandbox em producao: as cobrancas sao criadas "
            "localmente e nenhum dinheiro e movimentado de verdade."
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
