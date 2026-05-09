"""structlog + stdlib logging configuration.

One JSON line per request in prod; pretty console output in dev. Every log
record carries a ``request_id`` bound through ``contextvars`` so streamed
logs across async tasks stay correlated. The middleware that *sets* the
request ID lands in P3 (logging_mw.py); P1 just wires the plumbing.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog
from structlog.contextvars import merge_contextvars
from structlog.types import Processor

# Cross-coroutine request ID. The actual middleware that populates this is
# added in P3 — here we just expose the contextvar so other modules can read
# it without importing structlog directly.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure stdlib logging and structlog.

    Idempotent: safe to call from tests and from the FastAPI lifespan.

    Args:
        level: Root logger level name.
        fmt: ``"json"`` or ``"console"``.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Shared processors run for every log call regardless of renderer.
    shared_processors: list[Processor] = [
        merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if fmt == "json":
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logs (uvicorn, sqlalchemy, alembic, ...) through the same
    # renderer so we don't get a mix of formats in `docker logs`.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=shared_processors,
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Quiet down libs that are too chatty at INFO.
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger. Prefer this over ``logging.getLogger``."""
    return structlog.stdlib.get_logger(name)
