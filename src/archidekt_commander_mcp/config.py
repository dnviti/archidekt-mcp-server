from __future__ import annotations

from typing import Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


TransportMode = Literal["stdio", "sse", "streamable-http"]


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARCHIDEKT_MCP_",
        extra="ignore",
    )

    archidekt_base_url: str = "https://archidekt.com"
    scryfall_base_url: str = "https://api.scryfall.com"
    cache_ttl_seconds: int = Field(default=86400, ge=30, le=86400)
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_key_prefix: str = "archidekt-commander"
    http_timeout_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    max_search_results: int = Field(default=50, ge=1, le=100)
    scryfall_max_pages: int = Field(default=6, ge=1, le=20)
    user_agent: str = "archidekt-commander-mcp/0.2 (+mailto:replace-me@example.com)"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    transport: TransportMode = "streamable-http"
    streamable_http_path: str = "/mcp"
    stateless_http: bool = True

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> str:
        if value is None:
            return "INFO"
        return str(value).strip().upper() or "INFO"

    @field_validator("redis_url", mode="before")
    @classmethod
    def normalize_redis_url(cls, value: object) -> str:
        if value is None:
            return "redis://127.0.0.1:6379/0"
        return str(value).strip() or "redis://127.0.0.1:6379/0"

    @field_validator("redis_key_prefix", mode="before")
    @classmethod
    def normalize_redis_key_prefix(cls, value: object) -> str:
        if value is None:
            return "archidekt-commander"
        return str(value).strip() or "archidekt-commander"

    @computed_field
    @property
    def normalized_archidekt_base_url(self) -> str:
        return self.archidekt_base_url.rstrip("/")

    @computed_field
    @property
    def normalized_scryfall_base_url(self) -> str:
        return self.scryfall_base_url.rstrip("/")
