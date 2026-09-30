from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    database_url: str = "postgresql://lab:labpass@db:5432/labrelease"
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    db_connect_timeout: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
