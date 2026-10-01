from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    database_url: str = "postgresql://lab:lab@db:5432/lab"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    db_connect_timeout: float = 10.0
    db_acquire_timeout: float = 10.0
    # 串行化/死锁冲突时的事务重试次数
    tx_max_retries: int = 5
    tx_retry_backoff: float = 0.05


@lru_cache
def get_settings() -> Settings:
    return Settings()
