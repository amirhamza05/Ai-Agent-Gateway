"""Application settings.

All configuration comes from environment variables (loaded from ``.env`` by
``docker-compose``'s ``env_file:`` directive in dev, and from real env vars
in prod). Never hard-code secrets, and never log secret-typed fields.

The ``Settings`` class mirrors ``.env.example`` 1:1 — when you add a new
variable to the example, add the matching field here with a description.

Use :func:`get_settings` everywhere; it caches a single instance per process
so the env is parsed exactly once.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Bumped manually on every release. Surfaced via /healthz so operators can
# tell which build is running without exec'ing into the container.
APP_VERSION = "0.1.0"


class Settings(BaseSettings):
    """Process-wide configuration."""

    model_config = SettingsConfigDict(
        # `.env` is mounted by docker-compose; pydantic-settings will also
        # read it directly when running tests outside Docker.
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Postgres ------------------------------------------------------
    postgres_password: SecretStr = Field(
        ...,
        description="Postgres superuser password — must match the postgres container env.",
    )
    database_url: str = Field(
        ...,
        description="Async DSN, e.g. postgresql+asyncpg://gateway:...@postgres:5432/gateway",
    )
    test_database_url: str = Field(
        default="",
        description="Optional test DB DSN. Used by pytest fixtures only.",
    )

    # ---- Redis ---------------------------------------------------------
    redis_url: str = Field(
        default="redis://redis:6379/0",
        description="Redis URL for rate limiting and refresh-token blocklist.",
    )

    # ---- JWT -----------------------------------------------------------
    jwt_secret: SecretStr = Field(
        ...,
        description="HS256 signing secret. Generate with secrets.token_urlsafe(48).",
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT signing algorithm.")
    access_token_expires_min: int = Field(
        default=15,
        ge=1,
        description="Access-token lifetime in minutes.",
    )
    refresh_token_expires_days: int = Field(
        default=30,
        ge=1,
        description="Refresh-token lifetime in days.",
    )

    # ---- OpenRouter ----------------------------------------------------
    openrouter_api_key: SecretStr = Field(
        ...,
        description="OpenRouter API key. Server-side only — never sent to the add-in.",
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        description="OpenRouter base URL (override only for testing).",
    )

    # ---- Qdrant --------------------------------------------------------
    qdrant_url: str = Field(
        ...,
        description="Qdrant cluster URL, e.g. https://xxx.cloud.qdrant.io",
    )
    qdrant_api_key: SecretStr = Field(
        ...,
        description="Qdrant API key. Server-side only.",
    )

    # ---- Policy --------------------------------------------------------
    allowed_models: str = Field(
        default="",
        description="Comma-separated list of model IDs the gateway will forward.",
    )
    default_monthly_usd_cap: float = Field(
        default=10.00,
        ge=0,
        description="Default per-user monthly USD cap applied at register time.",
    )
    max_body_bytes: int = Field(
        default=32_000,
        ge=1024,
        description="Per-row truncation threshold for request_body and response_body.",
    )
    rate_limit_per_min: int = Field(
        default=30,
        ge=1,
        description="Per-user QPM allowed by the Redis token bucket.",
    )

    # ---- Logging -------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Root logger level.",
    )
    log_format: Literal["json", "console"] = Field(
        default="json",
        description="`json` for prod (machine-readable), `console` for dev (pretty).",
    )

    # ---- Server --------------------------------------------------------
    app_host: str = Field(default="0.0.0.0", description="Uvicorn bind host.")  # noqa: S104
    app_port: int = Field(default=8000, ge=1, le=65535, description="Uvicorn bind port.")
    app_workers: int = Field(default=2, ge=1, description="Uvicorn worker count.")

    public_hostname: str = Field(
        default="localhost",
        description="Public hostname Caddy serves (used in prod TLS provisioning).",
    )

    # ---- Derived -------------------------------------------------------
    @property
    def version(self) -> str:
        """Build version string returned by /healthz."""
        return APP_VERSION

    @property
    def allowed_models_list(self) -> list[str]:
        """Parsed view of ``allowed_models``."""
        return [m.strip() for m in self.allowed_models.split(",") if m.strip()]

    @property
    def allowed_models_set(self) -> frozenset[str]:
        """Allow-list of model IDs as a hashable set.

        Frozenset (vs. list) gives O(1) membership for the per-request
        validation in ``/v1/messages``. Cached implicitly because ``Settings``
        is itself ``lru_cache``d via :func:`get_settings`.
        """
        return frozenset(self.allowed_models_list)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached :class:`Settings` instance.

    Tests that need to override values should use FastAPI's
    ``app.dependency_overrides`` rather than mutating this cache.
    """
    return Settings()  # type: ignore[call-arg]
