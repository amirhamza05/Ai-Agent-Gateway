"""SQLAlchemy ORM models.

Phase 2 introduces the auth tables: ``users`` and ``refresh_tokens``.
Phase 3 adds ``request_log`` — the audit row written for every authenticated
upstream call. The plan (§7) treats ``request_log`` as the canonical billing
ledger: ``cost_usd`` is summed for the monthly cap query, and the
``(user_id, created_at DESC)`` index serves both the cap query and the
``/v1/usage`` summary.

All models inherit from :class:`Base` which is the single ``MetaData``
target Alembic's ``--autogenerate`` examines. Don't create alternate bases.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Project-wide declarative base.

    All ORM models inherit from this. Don't create alternate bases — Alembic
    autogenerate only looks at one ``MetaData`` instance.
    """


class User(Base):
    """A registered user.

    Email is stored lowercased (the routes layer normalises before INSERT
    and before lookup); we additionally enforce uniqueness at the column
    level so a race won't produce duplicates.

    ``monthly_usd_cap`` is a NUMERIC(10,4) so we can store fractions of a
    cent without rounding bias when summing in the cap-check query (P3).
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    monthly_usd_cap: Mapped[Decimal] = mapped_column(
        Numeric(10, 4),
        nullable=False,
        server_default=text("10.00"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    # Phase D — Admin Dashboard. Default FALSE so existing rows stay
    # non-admin after the migration runs. The bootstrap mechanism is the
    # ``gateway-admin promote <email>`` CLI; there is intentionally no
    # "first request creates an admin" behaviour because that's an
    # attack vector.
    is_admin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class RefreshToken(Base):
    """An issued refresh token.

    Only the SHA-256 hex of the raw token is persisted. Single-use rotation
    is implemented in ``gateway.auth.routes``: each successful /auth/refresh
    revokes the row it consumed and inserts a fresh one.

    ``revoked_at`` doubles as a kill switch for /auth/logout and for the
    reuse-detection flow that revokes all of a user's tokens when a
    previously-revoked token is presented.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    user: Mapped[User] = relationship("User", back_populates="refresh_tokens")

    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
    )


class RequestLog(Base):
    """One row per authenticated upstream call.

    Written from the streaming generator's ``finally`` block in
    ``gateway.routes.messages`` (and, in later phases, from non-streaming
    routes too). The row carries:

    * Identification — ``request_id`` (UUID, distinct from ``id``) is also
      echoed in the ``X-Request-Id`` response header so the operator can
      join client logs to gateway logs to this table.
    * Billing — ``model``, ``tokens_in``, ``tokens_out``, and the resolved
      ``cost_usd`` from ``billing.PRICES_PER_MTOKEN``. ``cost_usd`` is the
      ledger value summed by the monthly-cap query and ``/v1/usage``.
    * Audit — ``request_body`` (JSONB) and ``response_body`` (TEXT) carry
      truncated payloads up to ``MAX_BODY_BYTES``; the original sizes are
      preserved in ``request_bytes`` / ``response_bytes`` so logs stay
      truthful when bodies are clipped.
    * Forensics — ``status_code``, ``error_code``, ``latency_ms``,
      ``client_ip``, and a freeform ``meta`` JSONB (currently used for
      ``ttfb_ms``).
    * ``chat_id`` — optional opaque string supplied by the UI/add-in via
      the ``X-Chat-Id`` request header. Groups all steps of a single
      agent prompt into one logical conversation.

    Indexes (mirror §7 of the plan exactly):

    * ``(user_id, created_at DESC)`` — drives the monthly cap query and
      ``/v1/usage`` summaries. The DESC matches the access pattern (newest
      rows first) so Postgres can short-circuit on the index.
    * ``(created_at)`` — drives the (P4+) retention sweep.
    """

    __tablename__ = "request_log"

    # BIGSERIAL via Identity. ``always=False`` lets us still INSERT explicit
    # ids if a future migration needs to backfill, but the default supplies
    # one for normal traffic. ``request_id`` (the UUID) is the value the
    # client sees; ``id`` is purely an internal monotonic key.
    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=False),
        primary_key=True,
    )
    request_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    # Nullable per the plan: leaves the door open for future unauthenticated
    # endpoints that still want to log. P3 only logs authenticated calls so
    # in practice this is always populated.
    user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Anthropic prompt-cache tokens. ``cache_read_tokens`` are the tokens
    # served from cache (much cheaper than fresh input). ``cache_write_tokens``
    # are tokens written into the cache on this turn (slightly more expensive
    # than fresh input). Both are billed via ``model_pricing.cache_*``.
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Numeric(10,6) gives us $9999.999999 max with no float drift on summed
    # ledger values. Per-row cost is tiny — the precision is for the SUM().
    cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6),
        nullable=True,
    )
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_version: Mapped[str | None] = mapped_column(String, nullable=True)
    # INET on the Postgres side, str in Python. asyncpg/SQLAlchemy accept a
    # plain string for INSERT — no ipaddress.ip_address() conversion needed.
    client_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    request_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Optional agent/chat grouping key. Supplied by the client as the
    # ``X-Chat-Id`` header. All steps within a single agent invocation
    # share the same chat_id so operators can reconstruct the full
    # exchange from request_log without re-joining on timing.
    chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # DESC index on created_at matches the dominant access pattern
        # (newest first) for both the cap query and /v1/usage summaries.
        Index(
            "ix_request_log_user_created",
            "user_id",
            text("created_at DESC"),
        ),
        Index("ix_request_log_created_at", "created_at"),
        Index("ix_request_log_chat_id", "chat_id"),
    )


# ---- API Tokens (PC-locked) ------------------------------------------


class ApiToken(Base):
    """A long-lived API token issued to an ArcPy/add-in user.

    Workflow:
    1. A JWT-authenticated user (or admin) calls ``POST /auth/tokens`` to
       mint a token. The raw value is returned **once** and never persisted;
       only the SHA-256 hex digest is stored here.
    2. The ArcPy add-in calls ``POST /auth/token/connect`` with the raw
       token + a ``machine_id`` string (Windows machine GUID, MAC hash, or
       any stable hardware fingerprint supplied by the client).
    3. On first connect: ``machine_fingerprint`` is NULL → the gateway
       sets it to the provided machine_id and returns a short-lived JWT.
    4. On subsequent connects: ``machine_fingerprint`` must match the
       stored value → same JWT is returned. Any mismatch → 403
       ``machine_locked``.

    ``description`` and ``author`` are free-form human-readable labels
    supplied at creation time so operators can identify tokens in the
    dashboard without exposing the raw value.
    """

    __tablename__ = "api_tokens"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    # Set to the client-supplied machine identifier on first connect.
    # After that it is immutable — only the matching PC can use this token.
    machine_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    # When TRUE, the token may use any currently-allowed model in
    # ``model_pricing`` and auto-picks-up models added in the future.
    # When FALSE the allow-list is the join-table rows in
    # ``api_token_models``.
    allow_all_models: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    user: Mapped[User] = relationship("User")
    models: Mapped[list[ApiTokenModel]] = relationship(
        "ApiTokenModel",
        back_populates="token",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_api_tokens_user_id", "user_id"),
    )


class ApiTokenModel(Base):
    """Per-token model allow-list row.

    Only consulted when the parent ``api_tokens.allow_all_models`` is
    FALSE. Otherwise the token uses every currently-allowed model in
    ``model_pricing`` and the rows here are ignored.
    """

    __tablename__ = "api_token_models"

    token_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("api_tokens.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model: Mapped[str] = mapped_column(
        Text,
        ForeignKey("model_pricing.model", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    token: Mapped[ApiToken] = relationship("ApiToken", back_populates="models")

    __table_args__ = (
        Index("ix_api_token_models_token_id", "token_id"),
    )


# ---- Phase D — Admin Dashboard ---------------------------------------


class DashboardSession(Base):
    """A signed-in admin's dashboard session.

    Kept distinct from ``refresh_tokens`` because the cookie semantics are
    different (HttpOnly + 8-hour expiry rotated on activity, not the
    30-day single-use rotation of API refresh tokens). Only the SHA-256
    hex of the raw cookie value is persisted, so a leaked DB row cannot
    impersonate the admin.
    """

    __tablename__ = "dashboard_sessions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)

    __table_args__ = (
        Index("ix_dashboard_sessions_user_id", "user_id"),
        Index("ix_dashboard_sessions_expires_at", "expires_at"),
    )


class GatewaySettings(Base):
    """Admin-configurable credentials managed from the dashboard settings page.

    One row per key. Supported keys: ``openrouter_api_key``, ``qdrant_url``,
    ``qdrant_api_key``. A missing row means "fall back to the env var".
    Values are stored as plaintext — the DB is on the same machine as
    the ``.env`` file, so no additional exposure is introduced.
    """

    __tablename__ = "gateway_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class ModelPricing(Base):
    """Per-model pricing + allow-list, owned by the dashboard.

    Phase D moves pricing and model allow-listing out of in-process
    constants into the DB so the operator can edit pricing without a
    deploy. ``billing.PricingCache`` reads this table with a 30-second
    TTL; dashboard mutations call ``cache.invalidate()`` on commit.

    ``output_per_mtoken`` is nullable for embedding-only models —
    embeddings have no "output" tokens. ``disabled_at`` is the
    soft-delete marker; rows with ``disabled_at IS NOT NULL`` are
    treated as not-allowed even if ``is_allowed`` is still TRUE.
    """

    __tablename__ = "model_pricing"

    model: Mapped[str] = mapped_column(Text, primary_key=True)
    endpoint_kind: Mapped[str] = mapped_column(Text, nullable=False)
    input_per_mtoken: Mapped[Decimal] = mapped_column(
        Numeric(10, 4),
        nullable=False,
    )
    output_per_mtoken: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4),
        nullable=True,
    )
    # Prompt-cache pricing (Anthropic). ``cache_read_per_mtoken`` is the
    # discounted rate for tokens served from a cache hit; typically a tenth
    # of the regular input price. ``cache_write_per_mtoken`` is the premium
    # rate for tokens written into the cache on a given turn; typically
    # 1.25× the regular input price. Both NULL on embedding rows.
    cache_read_per_mtoken: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4),
        nullable=True,
    )
    cache_write_per_mtoken: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4),
        nullable=True,
    )
    is_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
