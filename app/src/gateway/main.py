"""FastAPI application factory.

This module is intentionally thin: it builds the app, wires the lifespan,
and registers routers. All real work lives in ``gateway.routes.*``,
``gateway.auth.*``, and ``gateway.upstream.*``. Don't add request handlers
or business logic here.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

import redis.asyncio as redis_asyncio
from fastapi import FastAPI

from gateway.auth.routes import router as auth_router
from gateway.config import Settings, get_settings
from gateway.db.session import create_engine, create_session_factory
from gateway.logging_setup import configure_logging, get_logger
from gateway.middleware import RequestSizeLimitMiddleware
from gateway.routes.embeddings import router as embeddings_router
from gateway.routes.health import router as health_router
from gateway.routes.messages import router as messages_router
from gateway.routes.models import router as models_router
from gateway.routes.qdrant import router as qdrant_router
from gateway.routes.tokens import router as tokens_router
from gateway.routes.usage import router as usage_router
from gateway.upstream.openrouter import build_client as build_openrouter_client
from gateway.upstream.qdrant import build_client as build_qdrant_client

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, AsyncSession

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open shared resources on startup and clean them up on shutdown.

    We attach the engine, session factory, and Redis client to ``app.state``
    so request handlers can reach them without re-creating per request.
    """
    settings: Settings = get_settings()

    # Captured here (not in create_app) so it reflects when the worker
    # actually started serving, not when the module was imported. Used
    # by the /dashboard/server uptime panel.
    app.state.started_monotonic = time.monotonic()

    configure_logging(level=settings.log_level, fmt=settings.log_format)
    logger.info("startup.begin", version=settings.version, log_format=settings.log_format)

    # ---- Postgres -------------------------------------------------------
    engine: AsyncEngine = create_engine(settings.database_url)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory

    # ---- Redis ----------------------------------------------------------
    # decode_responses=True so callers get str instead of bytes; the only
    # binary thing we'd store is a hash digest, and those are hex strings.
    redis_client = redis_asyncio.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    app.state.redis = redis_client

    # ---- Credential store ----------------------------------------------
    # 30-second TTL cache for gateway_settings (OpenRouter key, Qdrant
    # URL/key). Resolution order: DB row → env var → CredentialMissing.
    # Invalidated by the dashboard settings POST handler.
    from gateway.credential_store import CredentialStore
    credential_store = CredentialStore(settings, ttl_seconds=30.0)
    app.state.credential_store = credential_store

    # ---- Pricing cache -------------------------------------------------
    # 30-second in-process TTL snapshot of model_pricing. Shared across
    # all requests on this worker. Invalidated by dashboard mutation routes.
    from gateway.billing import PricingCache
    from sqlalchemy import func, select as _select
    from gateway.db.models import ModelPricing as _ModelPricing

    pricing_cache = PricingCache(ttl_seconds=30.0)
    app.state.pricing_cache = pricing_cache

    # Bootstrap warning: if the pricing table is empty, v1 endpoints will
    # fall back to the in-process constants and log a warning per call.
    # Wrapped in try/except so a cold-start before migrations run doesn't
    # crash the lifespan (the pricing cache handles the empty-table case
    # gracefully per its own fallback logic).
    try:
        async with session_factory() as _sess:
            _count_result = await _sess.execute(
                _select(func.count()).select_from(_ModelPricing)
            )
            _count = _count_result.scalar_one()
            if _count == 0:
                logger.critical(
                    "startup.pricing_table_empty",
                    msg=(
                        "pricing table empty — /v1/messages and /v1/embeddings "
                        "will reject every model"
                    ),
                )
    except Exception:
        logger.warning("startup.pricing_check_skipped", reason="could not reach DB at startup")

    # ---- OpenRouter HTTP client ----------------------------------------
    # One AsyncClient per process so the connection pool is shared across
    # all coroutines. Owned by the lifespan (not a module-global) so
    # shutdown is clean and tests can swap in a mocked transport.
    openrouter_client = build_openrouter_client()
    app.state.openrouter_client = openrouter_client

    # ---- Qdrant HTTP client --------------------------------------------
    # Separate from OpenRouter because (a) different timeout profile
    # (no streaming, faster reads), (b) different auth header shape
    # (``api-key:`` vs. ``Authorization: Bearer``), and (c) lifespan
    # parity makes it trivial for tests to swap in a respx-mocked
    # transport per app.
    qdrant_client = build_qdrant_client()
    app.state.qdrant_client = qdrant_client

    logger.info("startup.complete")

    try:
        yield
    finally:
        logger.info("shutdown.begin")
        # Close in reverse order of opening. HTTP clients first (they
        # might have in-flight requests that need to finish), Redis next,
        # then the SQL engine last.
        try:
            await qdrant_client.aclose()
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.exception("shutdown.qdrant_close_failed")

        try:
            await openrouter_client.aclose()
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.exception("shutdown.openrouter_close_failed")

        try:
            await redis_client.aclose()
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.exception("shutdown.redis_close_failed")

        try:
            await engine.dispose()
        except Exception:  # pragma: no cover
            logger.exception("shutdown.engine_dispose_failed")

        logger.info("shutdown.complete")


def create_app() -> FastAPI:
    """Build a fresh FastAPI app.

    Tests should call this rather than importing the module-level ``app`` so
    each test gets an isolated lifespan.
    """
    settings = get_settings()

    app = FastAPI(
        title="GeoSWMM Gateway",
        version=settings.version,
        description="Gateway between the GeoSWMM AI add-in and OpenRouter / Qdrant.",
        lifespan=lifespan,
        # Default JSON responses are already non-streaming; routes that need
        # SSE override that explicitly in P3.
        docs_url="/docs",
        redoc_url=None,
    )

    # Middleware ---------------------------------------------------------
    # P4 — request-side body cap. Generous (4× the per-row truncation
    # limit) so legitimate Anthropic Messages bodies fit, but bounded
    # so a malicious client can't OOM the worker by streaming 100 MB
    # of synthetic content. Order: ``add_middleware`` adds to the OUTER
    # layer, so this runs FIRST on the way in (before auth, before
    # routing), which is what we want for a body-size short-circuit.
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_bytes=settings.max_body_bytes * 4,
    )

    # ---- Jinja2 templates + filters ------------------------------------
    import datetime as _dt
    from pathlib import Path
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates

    templates_dir = Path(__file__).parent / "dashboard" / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    def _relative_time(dt: object) -> str:
        if dt is None:
            return "never"
        if not hasattr(dt, "tzinfo") or dt.tzinfo is None:  # type: ignore[union-attr]
            dt = dt.replace(tzinfo=_dt.timezone.utc)  # type: ignore[union-attr]
        now = _dt.datetime.now(_dt.timezone.utc)
        diff = now - dt  # type: ignore[operator]
        s = int(diff.total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"

    def _fmt_date(dt: object) -> str:
        if dt is None:
            return ""
        if hasattr(dt, "strftime"):
            return dt.strftime("%Y-%m-%d %H:%M")  # type: ignore[union-attr]
        return str(dt)

    from gateway.dashboard.server_stats import format_bytes as _format_bytes

    templates.env.filters["relative_time"] = _relative_time
    templates.env.filters["fmt_date"] = _fmt_date
    templates.env.filters["fmt_bytes"] = _format_bytes
    app.state.templates = templates

    # ---- Static assets -------------------------------------------------
    static_dir = Path(__file__).parent / "dashboard" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/dashboard/static",
        StaticFiles(directory=str(static_dir)),
        name="dashboard-static",
    )

    # Routers ------------------------------------------------------------
    # Health check has no prefix so probes hit /healthz directly.
    app.include_router(health_router)

    # P2 — Auth. P3 lands /v1/messages (streaming) and replaces /v1/usage's
    # placeholder with a real aggregate query. P5 adds /v1/embeddings and
    # /v1/qdrant/*.
    app.include_router(auth_router)
    app.include_router(tokens_router)
    app.include_router(usage_router)
    app.include_router(models_router)
    app.include_router(messages_router)
    app.include_router(embeddings_router)
    app.include_router(qdrant_router)

    # Phase D — Admin Dashboard (no /v1 prefix).
    from gateway.dashboard.routes import router as dashboard_router
    app.include_router(dashboard_router)

    return app


# Module-level instance for ``uvicorn gateway.main:app``. Tests should use
# ``create_app()`` instead so they get isolated state.
app = create_app()
