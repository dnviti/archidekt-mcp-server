from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationInfo, computed_field, field_validator
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
    personal_deck_cache_ttl_seconds: int = Field(default=900, ge=0, le=3600)
    archidekt_rate_limit_max_requests: int = Field(default=30, ge=1, le=1000)
    archidekt_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    archidekt_retry_max_attempts: int = Field(default=3, ge=1, le=10)
    archidekt_retry_base_delay_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    archidekt_exact_name_cache_ttl_seconds: int = Field(default=900, ge=0, le=86400)
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
    auth_enabled: bool = False
    public_base_url: str | None = None
    auth_code_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    auth_access_token_ttl_seconds: int | None = Field(default=None)
    auth_refresh_token_ttl_seconds: int | None = Field(default=None)

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> str:
        if value is None:
            return "INFO"
        return str(value).strip().upper() or "INFO"

    @field_validator("public_base_url", mode="before")
    @classmethod
    def normalize_public_base_url(cls, value: object) -> str | None:
        if value is None:
            return None
        compact = str(value).strip()
        return compact or None

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

    @field_validator("auth_access_token_ttl_seconds", "auth_refresh_token_ttl_seconds", mode="before")
    @classmethod
    def normalize_optional_auth_ttl(cls, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("Boolean values are not valid TTL settings.")
        if isinstance(value, int):
            numeric = value
        else:
            compact = str(value).strip().lower()
            if compact in {"", "0", "none", "null", "never", "infinite", "infinity", "disabled", "off"}:
                return None
            numeric = int(compact)
        if numeric <= 0:
            return None
        return numeric

    @field_validator("auth_access_token_ttl_seconds", "auth_refresh_token_ttl_seconds")
    @classmethod
    def validate_optional_auth_ttl(cls, value: int | None, info: ValidationInfo) -> int | None:
        if value is None:
            return None
        if info.field_name == "auth_access_token_ttl_seconds":
            if value < 300 or value > 2592000:
                raise ValueError("Access token TTL must be between 300 and 2592000 seconds, or disabled.")
            return value
        if value < 3600 or value > 31536000:
            raise ValueError("Refresh token TTL must be between 3600 and 31536000 seconds, or disabled.")
        return value

    @computed_field
    @property
    def normalized_archidekt_base_url(self) -> str:
        return self.archidekt_base_url.rstrip("/")

    @computed_field
    @property
    def normalized_scryfall_base_url(self) -> str:
        return self.scryfall_base_url.rstrip("/")

    @computed_field
    @property
    def normalized_public_base_url(self) -> str | None:
        if not self.public_base_url:
            return None
        return self.public_base_url.rstrip("/")
