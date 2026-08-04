from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    app_secret_key: str = Field(min_length=44)
    database_url: str = "sqlite:///./connection_topology.db"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    history_retention_days: int = Field(default=7, ge=1, le=3650)
    remote_timeout_seconds: int = Field(default=15, ge=1, le=120)
    scheduler_enabled: bool = True
    import_test_max_workers: int = Field(default=20, ge=1, le=200)
    scan_max_workers: int = Field(default=30, ge=1, le=200)
    scan_queue_size: int = Field(default=2000, ge=1, le=100000)
    scan_jitter_seconds: int = Field(default=300, ge=0, le=86400)
    sqlite_busy_timeout_ms: int = Field(default=30000, ge=1000, le=300000)
    sqlite_write_retry_delays: Annotated[tuple[float, ...], NoDecode] = (
        0.1,
        0.3,
        0.8,
        1.5,
        3.0,
    )
    db_pool_size: int = Field(default=20, ge=1, le=200)
    db_max_overflow: int = Field(default=10, ge=0, le=200)
    db_pool_timeout_seconds: int = Field(default=60, ge=1, le=300)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("sqlite_write_retry_delays", mode="before")
    @classmethod
    def parse_sqlite_write_retry_delays(cls, value):
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("SQLite 写入重试间隔不能为空")
            try:
                value = tuple(float(part.strip()) for part in value.split(","))
            except ValueError as exc:
                raise ValueError("SQLite 写入重试间隔必须是逗号分隔的秒数") from exc
        try:
            parsed = tuple(float(delay) for delay in value)
        except (TypeError, ValueError) as exc:
            raise ValueError("SQLite 写入重试间隔必须是秒数序列") from exc
        if not parsed or any(delay < 0 for delay in parsed):
            raise ValueError("SQLite 写入重试间隔必须至少包含一个非负秒数")
        return parsed


@lru_cache
def get_settings() -> Settings:
    return Settings()
