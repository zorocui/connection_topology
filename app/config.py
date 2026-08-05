from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    app_secret_key: str = Field(min_length=44)
    database_url: str
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    history_retention_days: int = Field(default=7, ge=1, le=3650)
    remote_timeout_seconds: int = Field(default=15, ge=1, le=120)
    scheduler_enabled: bool = True
    import_test_max_workers: int = Field(default=20, ge=1, le=200)
    scan_max_workers: int = Field(default=30, ge=1, le=200)
    scan_queue_size: int = Field(default=2000, ge=1, le=100000)
    scan_jitter_seconds: int = Field(default=300, ge=0, le=86400)
    web_workers: int | None = Field(default=None, ge=1, le=64)
    db_pool_size: int = Field(default=3, ge=1, le=50)
    db_max_overflow: int = Field(default=2, ge=0, le=50)
    db_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    db_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
    scan_lease_seconds: int = Field(default=90, ge=30, le=600)
    task_heartbeat_seconds: int = Field(default=15, ge=5, le=120)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("database_url")
    @classmethod
    def require_postgresql_psycopg(cls, value: str) -> str:
        url = make_url(value)
        if url.drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_URL 必须使用 postgresql+psycopg")
        return value

    @model_validator(mode="after")
    def heartbeat_precedes_lease(self):
        if self.task_heartbeat_seconds * 2 >= self.scan_lease_seconds:
            raise ValueError("TASK_HEARTBEAT_SECONDS 必须小于扫描租约的一半")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
