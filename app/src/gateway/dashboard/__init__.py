"""Phase D — Admin Dashboard.

Server-rendered HTML at ``/dashboard/*`` for the operator: user
management, model-pricing CRUD, request_log browsing, cost / usage
reports. Cookie-session auth gated by ``users.is_admin``.

Templates live in ``templates/``, static assets in ``static/``. The
router is registered from :mod:`gateway.main`'s ``create_app``.
"""

from __future__ import annotations

from gateway.dashboard.routes import router

__all__ = ["router"]
