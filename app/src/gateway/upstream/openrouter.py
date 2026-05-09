"""OpenRouter HTTP client.

The gateway holds a single :class:`httpx.AsyncClient` opened in the app
lifespan and stored on ``app.state.openrouter_client``. The streaming
``/v1/messages`` handler grabs it from ``request.app.state`` so a single
TCP connection pool is shared across all coroutines.

Design notes:

* **HTTP/1.1 only** (no h2). OpenRouter speaks HTTP/1.1; enabling h2 would
  add TLS handshake cycles for nothing on first connect. Keep-alive on
  HTTP/1.1 is enough for our concurrency target.
* **Long read timeout (120 s)** because chat streams can sit silent for a
  while between tokens when the model is "thinking". Connect/write/pool
  are tight to fail fast on transient network blips.
* **No module-level singleton** — owning the client in the lifespan means
  shutdown is clean (``await client.aclose()``) and tests can swap a
  mocked transport per app.

**Embeddings provider decision (P5).**
We route ``/v1/embeddings`` through the same OpenRouter client that serves
``/v1/messages``. Rationale: the auth headers, base URL, and HTTP transport
are identical to Messages, so a separate provider client would be pure
overhead for the trial month. OpenRouter exposes an OpenAI-shape
``/embeddings`` endpoint (``{"model": ..., "input": [...]}`` request,
``{"data":[{"embedding":[...]}], "usage":{"prompt_tokens":...}, ...}``
response). Default model: ``openai/text-embedding-3-small`` (1536 dims,
$0.020/M tokens). Swapping to Voyage or self-hosted later is a
one-function change in :func:`call_embeddings` — the route layer doesn't
care. See §16 of ``GatewayServerPlan.md`` for the resolved decision.
"""

from __future__ import annotations

from typing import Any

import httpx

from gateway.config import Settings


def build_client() -> httpx.AsyncClient:
    """Construct the gateway's OpenRouter HTTP client.

    Called once from the FastAPI lifespan in :mod:`gateway.main`. Caller
    is responsible for ``await client.aclose()`` on shutdown.

    Timeouts are tuned for SSE streaming:

    * ``connect=5`` — fail fast on DNS/TCP issues.
    * ``read=120`` — long enough to swallow silent gaps between tokens
      when the model is thinking. Anthropic's published p99 first-token
      latency is well under 5 s for Haiku, but quieter intra-stream gaps
      can hit 30 s+ on long responses.
    * ``write=10`` — request bodies are small (chat messages, max ~32 KB
      after our cap); 10 s is generous.
    * ``pool=5`` — never block more than 5 s waiting for a free
      connection from the pool.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
        # OpenRouter is HTTP/1.1. Enabling h2 buys nothing and adds ALPN
        # negotiation latency on cold connects.
        http2=False,
    )


def auth_headers(settings: Settings) -> dict[str, str]:
    """Return the Authorization + identifying headers for OpenRouter.

    OpenRouter encourages identifying headers (``HTTP-Referer``,
    ``X-Title``) so they can attribute traffic in their dashboard. Neither
    is sensitive — the OpenRouter key is the one secret here.

    Caller responsibility: do NOT pass through the inbound request's
    ``Authorization`` header. We use the gateway's server-side key, never
    the user's bearer.
    """
    return {
        "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}",
        "HTTP-Referer": "https://geoswmm-gateway",
        "X-Title": "GeoSWMM Gateway",
    }


async def call_embeddings(
    client: httpx.AsyncClient,
    *,
    settings: Settings,
    model: str,
    inputs: list[str],
) -> tuple[httpx.Response, dict[str, Any] | None]:
    """POST ``{base_url}/embeddings`` and return ``(response, parsed_json)``.

    OpenRouter mirrors the OpenAI embeddings shape:

    * Request body — ``{"model": ..., "input": [...]}``.
    * Response body — ``{"data":[{"embedding":[...], "index": N}, ...],
      "usage": {"prompt_tokens": N, "total_tokens": N}, "model": ...}``.

    This helper does **not** raise on 4xx/5xx — the caller logs the row and
    surfaces the upstream status to the client verbatim, mirroring the
    streaming path's "tee error to client AND record" discipline. JSON
    decode failures return ``None`` for the parsed dict so the caller can
    still log status/bytes for non-JSON error pages.

    The shared :class:`httpx.AsyncClient` is the gateway's pooled
    OpenRouter client — pass ``request.app.state.openrouter_client``.
    """
    url = f"{settings.openrouter_base_url}/embeddings"
    body: dict[str, Any] = {"model": model, "input": inputs}

    resp = await client.post(url, json=body, headers=auth_headers(settings))

    parsed: dict[str, Any] | None
    try:
        parsed = resp.json()
    except (ValueError, httpx.DecodingError):
        # Non-JSON error pages (HTML 5xx, etc.) shouldn't crash the route
        # — the caller will log raw bytes via ``resp.content`` instead.
        parsed = None
    return resp, parsed
