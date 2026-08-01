from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "PedeAqui API"
    APP_ENV: str = "development"
    DEBUG: bool = True

    # Deixe em None para seguir o APP_ENV (desligado em producao).
    # Defina explicitamente para forcar um dos dois lados.
    ENABLE_API_DOCS: bool | None = None

    DATABASE_URL: str

    SUPABASE_URL: str
    SUPABASE_STORAGE_BUCKET: str = "restaurant-assets"

    # DEPRECIADA na Fase 1. Nenhuma rota HTTP usa mais esta chave: as rotas
    # /admin passaram a exigir JWT de lojista. Continua opcional aqui apenas
    # para que um .env antigo nao derrube o boot; pode ser removida do
    # ambiente depois que a Fase 1 estiver no ar.
    INTERNAL_API_KEY: str | None = None
    CUSTOMER_AUTH_SECRET: str
    CUSTOMER_JWT_SECRET: str | None = None
    CUSTOMER_ACCESS_TOKEN_MINUTES: int = 10080
    PASSWORD_RESET_TOKEN_MINUTES: int = 15

    # Segredo dos tokens de lojista. Vazio = usa CUSTOMER_AUTH_SECRET.
    ADMIN_AUTH_SECRET: str | None = None
    # Jornada de lojista e turno de trabalho, nao sessao de semanas como a do
    # cliente: o token do painel expira em 12h.
    ADMIN_ACCESS_TOKEN_MINUTES: int = 720

    RESEND_API_KEY: str | None = None
    EMAIL_FROM: str = "Rapidex <no-reply@pederapidex.com>"
    EMAIL_CODE_SECRET: str
    PASSWORD_RESET_SECRET: str

    OPENAI_API_KEY: str
    MODEL_NAME: str = "gpt-5-mini"
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    GOOGLE_MAPS_ROUTES_API_KEY: str = ""
    GOOGLE_MAPS_ROUTES_BASE_URL: str = "https://routes.googleapis.com"
    GOOGLE_MAPS_TIMEOUT_SECONDS: float = 5
    GOOGLE_MAPS_ROUTING_PREFERENCE: str = "TRAFFIC_AWARE"
    DELIVERY_ESTIMATE_CACHE_TTL_SECONDS: int = 600
    DELIVERY_ESTIMATE_NEGATIVE_CACHE_TTL_SECONDS: int = 120
    DELIVERY_ESTIMATE_PROVIDER: str = "google_routes"
    # Por quanto tempo a estimativa guardada pode ser reaproveitada na
    # criacao do pedido, evitando refazer geocode + rota (as duas chamadas
    # pagas do Google). 15 minutos cobre o tempo de checkout com folga e
    # nao chega a valer para a proxima faixa de horario da filial, cujo
    # tempo de preparo e outro.
    DELIVERY_ESTIMATE_REUSE_TTL_SECONDS: int = 900
    REDIS_URL: str | None = None

    # Teto do corpo da requisicao. O maior payload legitimo e a criacao de
    # pedido, que com os limites de order_schema fica bem abaixo disso.
    MAX_REQUEST_BODY_BYTES: int = 262_144

    # Janela em que reenviar a mesma Idempotency-Key devolve a resposta
    # gravada em vez de criar de novo. Passado o TTL a chave e reciclavel.
    IDEMPOTENCY_TTL_HOURS: int = 24
    # False: requisicao sem o header passa sem protecao (so warning no log).
    # Ligue quando nao houver mais cliente antigo em campo — ver
    # normalize_idempotency_key.
    IDEMPOTENCY_REQUIRED: bool = False

    # Gateway de pagamento.
    #
    # "sandbox" e o provider interno: cria a cobranca localmente e aceita
    # webhook assinado com PAYMENT_WEBHOOK_SECRET, sem chamada externa. E o
    # que permite exercitar o fluxo inteiro sem depender do Mercado Pago
    # responder. "mercadopago" chama a API de verdade (ver
    # src/integrations/payment_gateway.py) usando a credencial cadastrada
    # do restaurante do pedido.
    PAYMENT_PROVIDER: str = "sandbox"
    # Segredo do HMAC do webhook do sandbox. Sem ele o webhook e recusado
    # com 503 — nao existe modo "aceita sem verificar".
    PAYMENT_WEBHOOK_SECRET: str | None = None
    # Segredo da assinatura do webhook do Mercado Pago. GLOBAL por enquanto,
    # nao por restaurante — funciona para o piloto (um restaurante, uma
    # conta). Cada restaurante tem sua PROPRIA conta no Mercado Pago (nao e
    # um app de marketplace com OAuth compartilhado) e configura o segredo
    # de notificacao dela mesma no proprio painel; com o segundo
    # restaurante, este segredo precisa passar a ser cadastrado por
    # restaurante, do mesmo jeito que o access_token em
    # restaurant_payment_credentials — nao fizemos isso ainda porque nao foi
    # pedido.
    MERCADOPAGO_WEBHOOK_SECRET: str | None = None

    # Qual conjunto de credencial usar: a de teste (para desenvolver sem
    # mover dinheiro de verdade) ou a de producao. Global e nao por
    # restaurante — todo mundo migra junto no dia do go-live. Trocar aqui
    # nao muda nenhuma linha de codigo, so exige que a credencial de
    # "production" ja esteja cadastrada (ver
    # scripts/register_restaurant_payment_credential.py).
    MERCADOPAGO_ENVIRONMENT: str = "test"
    # Chave Fernet usada para cifrar/decifrar o access_token de cada
    # restaurante em restaurant_payment_credentials. E o unico lugar em que
    # essa chave existe fora do processo que a gerou — perde-la torna toda
    # credencial cadastrada ilegivel, entao troque-a com um plano de
    # recadastro em maos, nunca de surpresa.
    PAYMENT_CREDENTIALS_ENCRYPTION_KEY: str | None = None

    RATE_LIMIT_ENABLED: bool = True
    # Cabecalho com o IP real do cliente. Atras do Traefik o socket peer e o
    # proxy; deixe vazio apenas se a API for exposta sem proxy na frente.
    RATE_LIMIT_CLIENT_IP_HEADER: str = "x-real-ip"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.strip().lower() in {"production", "prod"}

    @property
    def api_docs_enabled(self) -> bool:
        if self.ENABLE_API_DOCS is not None:
            return self.ENABLE_API_DOCS
        return not self.is_production

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        enable_decoding=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
