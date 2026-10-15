from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEDGER_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://ledger:ledger@localhost:5432/ledgercore"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_s: float = 30.0
    kafka_bootstrap_servers: str = "localhost:9092"
    outbox_topic: str = "ledgercore.events"
    outbox_batch_size: int = 100
    outbox_poll_interval_s: float = 0.5
    payout_stale_after_s: float = 60.0
    payout_recovery_interval_s: float = 5.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
