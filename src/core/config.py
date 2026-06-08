from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "PedeAqui API"
    APP_ENV: str = "development"
    DEBUG: bool = True

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
