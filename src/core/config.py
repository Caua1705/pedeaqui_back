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

    INTERNAL_API_KEY: str
    CUSTOMER_AUTH_SECRET: str
    CUSTOMER_JWT_SECRET: str | None = None
    CUSTOMER_ACCESS_TOKEN_MINUTES: int = 10080
    PASSWORD_RESET_TOKEN_MINUTES: int = 15

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
