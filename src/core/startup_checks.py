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
        # A credencial em si (access_token e webhook_secret) e por
        # restaurante e vive no banco (ver restaurant_payment_credentials);
        # o que da para checar no boot, sem consultar o banco, e se existe
        # chave para decifra-la. Sem isso TODA cobranca online responde 503
        # e TODO webhook responde 503, restaurante cadastrado ou nao.
        errors.append(
            "PAYMENT_PROVIDER=mercadopago sem PAYMENT_CREDENTIALS_ENCRYPTION_KEY. "
            "Nenhuma credencial de restaurante (access_token ou segredo de "
            "webhook) pode ser decifrada, e toda cobranca online e todo "
            "webhook responderiam 503. Gere uma chave com "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "ou volte PAYMENT_PROVIDER para sandbox."
        )

    if settings.ADMIN_AUTH_SECRET.strip() == settings.CUSTOMER_AUTH_SECRET.strip():
        # A variavel ser obrigatoria (config.py) resolve o caso de estar
        # ausente; este erro cobre o outro jeito de chegar ao mesmo lugar,
        # que e copiar o valor do cliente para preencher o campo. Token
        # forjado de um publico passaria a valer no outro.
        errors.append(
            "ADMIN_AUTH_SECRET e CUSTOMER_AUTH_SECRET tem o MESMO valor: os "
            "tokens de lojista e de cliente ficam assinados com a mesma "
            "chave, e comprometer um segredo compromete os dois publicos. "
            "Gere um valor proprio com "
            '`python -c "import secrets; print(secrets.token_urlsafe(48))"`.'
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

    if not settings.SUPABASE_SERVICE_ROLE_KEY:
        # Warning e nao erro: sem a chave so o upload de imagem do painel
        # para de funcionar (responde 503). Todo o resto da API, inclusive
        # a LEITURA das imagens ja existentes, continua igual — o bucket e
        # publico para leitura.
        warnings.append(
            "SUPABASE_SERVICE_ROLE_KEY nao definida: o upload de imagem do "
            "painel (POST /admin/products/{id}/image) responde 503. O "
            "restante do cardapio e as imagens ja enviadas nao sao afetados."
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

    # Nao ha warning de boot equivalente para o segredo de webhook do
    # Mercado Pago: ele e por restaurante e vive no banco (ver
    # restaurant_payment_credentials.webhook_secret_encrypted), entao nao
    # da para saber no boot se "algum" restaurante esta sem cadastrar —
    # mesma razao pela qual nao ha warning de boot para access_token
    # ausente. Quem nao tiver cadastrado descobre no primeiro webhook, que
    # responde 503.

    if settings.is_production and settings.PAYMENT_PROVIDER == "sandbox":
        warnings.append(
            "PAYMENT_PROVIDER=sandbox em producao: as cobrancas sao criadas "
            "localmente e nenhum dinheiro e movimentado de verdade."
        )

    header = settings.RATE_LIMIT_CLIENT_IP_HEADER.strip()
    if not header:
        warnings.append(
            "RATE_LIMIT_CLIENT_IP_HEADER vazio: atras de um proxy o rate "
            "limiting vai agrupar todos os clientes no IP do proxy."
        )
    elif settings.is_production:
        # Nao da para conferir no boot se o proxy preenche o cabecalho — isso
        # so aparece na primeira requisicao. O aviso existe para que a
        # dependencia esteja escrita em algum lugar que alguem le no deploy:
        # com o cabecalho ausente, `client_ip` manda todo mundo para o balde
        # compartilhado e o login publico passa a estourar 429 no primeiro
        # minuto de movimento. Confira ANTES de subir (docs/operacao.md).
        warnings.append(
            f"RATE_LIMIT_CLIENT_IP_HEADER={header}: o proxy na frente PRECISA "
            "sobrescrever esse cabecalho em toda requisicao. Se ele nao vier, "
            "todos os clientes caem num balde unico e as rotas publicas "
            "comecam a responder 429."
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
