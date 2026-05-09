"""Async ``httpx`` wrapper for Qdrant Cloud REST.

Doc: https://qdrant.tech/documentation/concepts/points/#search-points

Auth: header ``api-key: <key>``.
Base URL: ``settings.qdrant_url`` (e.g. ``https://....cloud.qdrant.io``).

For P5 we expose two passthrough operations:

* ``POST {base}/collections/{collection}/points/search`` (search)
* ``PUT  {base}/collections/{collection}/points`` (upsert, optional
  ``?wait=true``)

The collection name is **user-controlled** via the request body. We
validate it against a strict regex (``^[A-Za-z0-9_\\-]{1,64}$``) before
interpolating into the URL so a malicious caller can't inject ``..`` or a
fully-qualified URL into the path. Anything outside the regex returns
400 to the caller and never reaches Qdrant.

Design notes (mirror the OpenRouter client):

* **HTTP/1.1 only** — Qdrant Cloud speaks HTTP/1.1; h2 buys nothing.
* **No streaming** — search/upsert are simple JSON-in / JSON-out, so the
  ``read=30s`` timeout is generous; we don't need the long messages-style
  read window.
* **No module-level singleton** — the client is owned by the FastAPI
  lifespan in :mod:`gateway.main`. Tests can swap a mocked transport per
  app.
* **Inbound Authorization stripped at the route layer** — we forward only
  the gateway's server-side ``api-key``. Never echo the user's bearer.
"""

from __future__ import annotations

import re

import httpx
from fastapi import HTTPException, status

from gateway.config import Settings


# Strict per the plan: alphanumerics, underscore, dash. Cap at 64 chars
# so we don't dump a megabyte of crap into a URL via a misuse. The regex
# is *anchored* so partial matches (e.g. ``foo/../bar``) are rejected.
_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def build_client() -> httpx.AsyncClient:
    """Construct the gateway's Qdrant HTTP client.

    Called once from the FastAPI lifespan in :mod:`gateway.main`. Caller
    is responsible for ``await client.aclose()`` on shutdown.

    Timeouts:

    * ``connect=5`` — fail fast on DNS/TCP issues.
    * ``read=30`` — Qdrant search/upsert is sub-second on small payloads;
      a 30 s ceiling is generous and keeps a misbehaving cluster from
      pinning a worker.
    * ``write=10`` — request bodies are small (vectors + filters).
    * ``pool=5`` — never block more than 5 s waiting for a free
      connection.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        http2=False,
    )


def auth_headers(settings: Settings) -> dict[str, str]:
    """Return the headers Qdrant requires for an authenticated request.

    Caller responsibility: do NOT pass through the inbound
    ``Authorization`` header. We use the gateway's server-side Qdrant
    key, never the user's bearer.
    """
    return {
        "api-key": settings.qdrant_api_key.get_secret_value(),
        "Content-Type": "application/json",
    }


def validate_collection(name: str) -> None:
    """Reject collection names that don't match the strict allow-pattern.

    Raises :class:`fastapi.HTTPException` with 400 +
    ``{"error": "invalid_collection_name"}`` so the route layer doesn't
    need to repeat this check (it just calls this helper before building
    the URL).
    """
    if not _COLLECTION_RE.match(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_collection_name"},
        )


def search_url(settings: Settings, collection: str) -> str:
    """Build the points-search URL for ``collection``.

    Caller MUST have already called :func:`validate_collection` — this
    function does NOT re-validate, so a typo upstream could let an
    invalid name through. We don't double-check here because routes are
    expected to validate at body-parse time and surface the 400 with
    the rest of their request validation.
    """
    base = settings.qdrant_url.rstrip("/")
    return f"{base}/collections/{collection}/points/search"


def upsert_url(settings: Settings, collection: str, *, wait: bool = True) -> str:
    """Build the points-upsert URL for ``collection``.

    Qdrant accepts ``wait`` as a query parameter to switch between
    fire-and-forget and synchronous indexing. We surface that to the
    caller via the request body so the gateway's URL construction stays
    a function of validated data.
    """
    base = settings.qdrant_url.rstrip("/")
    wait_q = "true" if wait else "false"
    return f"{base}/collections/{collection}/points?wait={wait_q}"
