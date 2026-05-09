"""ASGI middleware: incoming-request safety nets.

P3 already truncates *response* bodies inside the streaming
generator. P4 adds the symmetric protection on the request side: a
malicious client must not be able to POST 100 MB of synthetic
``messages`` content and OOM the worker before the handler even
runs.

We implement this as raw ASGI rather than as Starlette's
``BaseHTTPMiddleware`` because the latter buffers the entire body
into memory before handing it to the next layer — defeating the
whole point of a size cap. Raw ASGI lets us either:

* short-circuit on ``Content-Length`` if the header is present and
  honest, or
* count bytes lazily as the body streams and slam the door (413 +
  close) the instant the running total exceeds the cap, for
  ``Transfer-Encoding: chunked`` requests with no length advertised.

Wired in :mod:`gateway.main` with ``max_bytes = settings.max_body_bytes
* 4`` so the request cap is generous (it has to fit the model name,
the messages array, and any tool definitions) but still bounded.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # pragma: no cover
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)


_REJECT_BODY = json.dumps(
    {"detail": {"error": "request_too_large"}}
).encode("utf-8")


class RequestSizeLimitMiddleware:
    """Reject requests whose body exceeds ``max_bytes``.

    Two enforcement modes — both compose with downstream middleware
    correctly because we own the ``receive`` side of the ASGI
    contract:

    * **Eager (Content-Length present)** — peek the header, compare,
      reject before invoking the inner app. No body bytes flow.
    * **Streaming (chunked / no length)** — wrap the ``receive``
      callable and tally ``http.request`` body bytes as they arrive.
      If the running total exceeds ``max_bytes`` we send a 413 and
      end the response. The inner app sees an
      ``http.disconnect`` so its own ``await request.body()`` (or
      similar) unblocks rather than hanging.

    Notes:

    * Only ``http`` scopes are gated; websockets and lifespan pass
      through untouched.
    * We do NOT buffer the body — the wrapped ``receive`` yields
      messages one at a time, same as the unwrapped one. The cap
      enforcement is just a counter + a kill-switch.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan + websocket scopes pass through untouched.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # ---- Eager check: Content-Length is honest most of the time.
        content_length = _content_length(scope)
        if content_length is not None and content_length > self.max_bytes:
            logger.info(
                "middleware.request_too_large",
                content_length=content_length,
                max_bytes=self.max_bytes,
                path=scope.get("path"),
            )
            await _send_413(send)
            # Drain whatever the client is still trying to send so
            # the connection doesn't hang on close. Best-effort —
            # bail when we see ``http.disconnect`` or run out of
            # body.
            await _drain(receive)
            return

        # ---- Streaming check: tally bytes as they arrive.
        # We only need the wrapper when no length was advertised
        # (chunked encoding). When Content-Length was honest and
        # under the cap, the inner app can read raw.
        if content_length is None:
            received = 0
            limit = self.max_bytes
            tripped = {"value": False}

            async def receive_with_cap() -> Message:
                nonlocal received
                if tripped["value"]:
                    # Inner app shouldn't keep reading after we've
                    # rejected, but guard against misbehaved code.
                    return {"type": "http.disconnect"}

                message = await receive()
                if message["type"] == "http.request":
                    body = message.get("body", b"") or b""
                    received += len(body)
                    if received > limit:
                        tripped["value"] = True
                        logger.info(
                            "middleware.request_too_large_streamed",
                            bytes_received=received,
                            max_bytes=limit,
                            path=scope.get("path"),
                        )
                        await _send_413(send)
                        return {"type": "http.disconnect"}
                return message

            await self.app(scope, receive_with_cap, send)
            return

        # Length present and within cap → no wrapping needed.
        await self.app(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    """Parse ``content-length`` from raw ASGI scope headers, if present.

    Headers are ``list[tuple[bytes, bytes]]`` in ASGI scope, lowercased
    by the protocol server. We tolerate junk values (non-integer)
    by treating them as "unknown length" — the streaming path will
    catch over-sized bodies anyway.
    """
    for raw_name, raw_value in scope.get("headers") or ():
        if raw_name == b"content-length":
            try:
                return int(raw_value.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                return None
    return None


async def _send_413(send: Send) -> None:
    """Emit a minimal 413 response.

    Headers and body are byte-stable so tests can pin them. The
    ``Connection: close`` hint isn't strictly necessary on HTTP/1.1
    but matches what most reverse proxies (Caddy included) expect
    for a request-too-large rejection.
    """
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_REJECT_BODY)).encode("ascii")),
                (b"connection", b"close"),
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": _REJECT_BODY,
            "more_body": False,
        }
    )


async def _drain(receive: Receive) -> None:
    """Best-effort drain of an in-flight body so the socket can close.

    Bounded by ``http.disconnect`` and by the absence of more body —
    we don't loop forever on a misbehaving client. Errors are
    swallowed because we've already committed to the 413 and don't
    want a cleanup exception to mask the real outcome.
    """
    try:
        for _ in range(64):  # hard upper bound — chunks should be few
            message = await receive()
            mtype = message.get("type")
            if mtype == "http.disconnect":
                return
            if mtype == "http.request" and not message.get("more_body", False):
                return
    except Exception:  # pragma: no cover - best-effort
        return
