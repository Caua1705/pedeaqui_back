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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        enable_decoding=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
