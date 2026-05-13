"""Dashboard route handlers — all ``/dashboard/*`` endpoints.

Every handler except ``/dashboard/login`` and ``/dashboard/logout`` is
gated behind :func:`gateway.dashboard.auth.require_admin`.

Flash messages are passed via a short-lived signed ``_flash`` cookie
(max_age=10 s, path=/dashboard) so redirects can carry a one-shot
message to the next GET.

CSRF protection: every POST (except login) calls
:func:`gateway.dashboard.csrf.verify_csrf` and returns
``{"error": "csrf_invalid"}`` on failure. Login POST is exempt because
there is no session to bind a CSRF token to — SameSite=Lax covers the
most common cross-site vector for the login form.

DB access uses
``async with request.app.state.db_session_factory() as session:``
rather than a FastAPI Depends, because ``require_admin`` already opens
one short-lived session and the route handler often needs its own.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import re as _re
import secrets
import uuid as _uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.jwt import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
    refresh_token_expiry,
)
from gateway.auth.passwords import hash_password, verify_password
from gateway.billing import PricingCache
from gateway.config import get_settings
from gateway.dashboard import auth as dash_auth
from gateway.dashboard import csrf as dash_csrf
from gateway.dashboard.reports import (
    cost_over_time,
    errors_over_time,
    latency_percentiles,
    to_json,
    top_users,
)
from gateway.dashboard import server_stats
from gateway.credential_store import (
    CredentialStore,
    SETTING_OPENROUTER_KEY,
)
from gateway.db.models import (
    ApiToken,
    ApiTokenModel,
    DashboardSession,
    Embedding,
    GatewaySettings,
    ModelPricing,
    RefreshToken,
    RequestLog,
    User,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_FLASH_SALT = "dashboard-flash"
_PAGE_SIZE_DEFAULT = 50
_PAGE_SIZE_MAX = 200

# Cache-buster for /dashboard/static/* assets. Computed once at import time
# from the dashboard.css mtime so browsers re-fetch CSS after every redeploy
# (image rebuild → new mtime) without users needing a hard refresh.
_STATIC_DIR = Path(__file__).parent / "static"


def _compute_static_version() -> str:
    try:
        mtime = (_STATIC_DIR / "dashboard.css").stat().st_mtime
        return str(int(mtime))
    except OSError:
        return "0"


_STATIC_VERSION = _compute_static_version()


# ---------------------------------------------------------------------------
# Flash helpers
# ---------------------------------------------------------------------------


def _flash_serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret, salt=_FLASH_SALT)


def _set_flash(response: Response, *, secret: str, message: str, kind: str = "info") -> None:
    """Attach a flash cookie to the response."""
    payload = _flash_serializer(secret).dumps({"msg": message, "kind": kind})
    response.set_cookie(
        key="_flash",
        value=payload,
        max_age=10,
        httponly=True,
        samesite="lax",
        path="/dashboard",
    )


def _pop_flash(request: Request, *, secret: str) -> dict[str, str] | None:
    """Read and clear the flash cookie from the request."""
    raw = request.cookies.get("_flash")
    if not raw:
        return None
    try:
        payload = _flash_serializer(secret).loads(raw)
        if isinstance(payload, dict):
            return payload
    except BadSignature:
        pass
    return None


def _redirect(url: str, *, flash_msg: str | None = None, flash_kind: str = "info",
              secret: str | None = None) -> RedirectResponse:
    """Build a 303 redirect, optionally attaching a flash cookie."""
    resp = RedirectResponse(url=url, status_code=303)
    if flash_msg and secret:
        _set_flash(resp, secret=secret, message=flash_msg, kind=flash_kind)
    return resp


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------


def _base_context(request: Request, *, secret: str) -> dict[str, Any]:
    """Return context fields common to every rendered template.

    Includes a ``csrf_token`` bound to the current admin session so the
    nav's logout form always has a valid token without every individual
    handler needing to mint one separately.
    """
    flash = _pop_flash(request, secret=secret)
    user: User | None = getattr(request.state, "dashboard_user", None)
    session_row: DashboardSession | None = getattr(request.state, "dashboard_session", None)
    csrf_token = ""
    if user is not None and session_row is not None:
        csrf_token = dash_csrf.issue_csrf(
            secret=secret,
            user_id=user.id,
            session_id=session_row.id,
        )
    ctx: dict[str, Any] = {
        "request": request,
        "flash": flash,
        "current_admin": user,
        "csrf_token": csrf_token,
        "static_version": _STATIC_VERSION,
    }
    return ctx


def _csrf_token(request: Request, *, secret: str) -> str:
    """Mint a CSRF token for the current admin's session."""
    user: User = request.state.dashboard_user
    session_row: DashboardSession = request.state.dashboard_session
    return dash_csrf.issue_csrf(
        secret=secret,
        user_id=user.id,
        session_id=session_row.id,
    )


def _check_csrf(form: dict[str, Any], *, request: Request, secret: str) -> bool:
    """Return True iff the CSRF token in ``form`` is valid for this session."""
    user: User = request.state.dashboard_user
    session_row: DashboardSession = request.state.dashboard_session
    token = form.get("csrf_token", "")
    return dash_csrf.verify_csrf(
        secret=secret,
        token=token,
        user_id=user.id,
        session_id=session_row.id,
    )


def _csrf_invalid() -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": "csrf_invalid"})


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------


def _paginate(total: int, page: int, size: int) -> dict[str, Any]:
    total_pages = max(1, math.ceil(total / size))
    page = max(1, min(page, total_pages))
    return {
        "page": page,
        "size": size,
        "total": total,
        "total_pages": total_pages,
        "offset": (page - 1) * size,
    }


# ===========================================================================
# Auth
# ===========================================================================


@router.get("/login")
async def get_login(request: Request) -> Response:
    """Render the login form."""
    settings = get_settings()
    return request.app.state.templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "flash": _pop_flash(request, secret=settings.jwt_secret.get_secret_value()),
            "static_version": _STATIC_VERSION,
        },
    )


@router.post("/login")
async def post_login(request: Request) -> Response:
    """Verify credentials, issue session cookie, redirect to overview."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = await request.form()
    email = str(form.get("email", "")).lower().strip()
    password = str(form.get("password", ""))

    async with request.app.state.db_session_factory() as session:
        result = await session.execute(
            select(User).where(User.email == email, User.is_active.is_(True))
        )
        user = result.scalar_one_or_none()

        if user is None or not verify_password(password, user.password_hash):
            resp = _redirect(
                "/dashboard/login",
                flash_msg="Invalid email or password.",
                flash_kind="error",
                secret=secret,
            )
            logger.warning("dashboard.login_failed", email=email)
            return resp

        if not user.is_admin:
            resp = _redirect(
                "/dashboard/login",
                flash_msg="Your account does not have admin access.",
                flash_kind="error",
                secret=secret,
            )
            logger.warning("dashboard.login_not_admin", user_id=str(user.id))
            return resp

        raw_token, expires_at = await dash_auth.create_session(
            session, user_id=user.id, request=request
        )
        await session.commit()

    resp = _redirect("/dashboard/")
    dash_auth.set_session_cookie(resp, raw_token, request=request)
    logger.info("dashboard.login_success", user_id=str(user.id))
    return resp


@router.post("/logout")
async def post_logout(request: Request) -> Response:
    """Revoke the current session cookie and redirect to login."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    raw_token = request.cookies.get(dash_auth.SESSION_COOKIE_NAME, "")

    async with request.app.state.db_session_factory() as session:
        await dash_auth.revoke_session(session, raw_token=raw_token)
        await session.commit()

    resp = _redirect(
        "/dashboard/login",
        flash_msg="Logged out successfully.",
        flash_kind="info",
        secret=secret,
    )
    dash_auth.clear_session_cookie(resp, request=request)
    # Clear flash from the cookie we just set on the redirect target
    return resp


# ===========================================================================
# Overview
# ===========================================================================


@router.get("/")
async def overview(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Dashboard overview page — KPI cards."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    async with request.app.state.db_session_factory() as session:
        # Today's stats
        today_stats = await session.execute(
            text(
                """
                SELECT
                  COALESCE(SUM(cost_usd), 0) AS today_cost,
                  COUNT(*) AS today_requests,
                  COUNT(*) FILTER (WHERE status_code >= 400) AS today_errors
                FROM request_log
                WHERE created_at >= CURRENT_DATE
                """
            )
        )
        row = today_stats.one()
        today_cost = float(row.today_cost or 0)
        today_requests = int(row.today_requests or 0)
        today_errors = int(row.today_errors or 0)
        today_error_rate = (today_errors / today_requests * 100) if today_requests > 0 else 0.0

        # Top model today
        top_model_result = await session.execute(
            text(
                """
                SELECT model, COUNT(*) AS n FROM request_log
                WHERE created_at >= CURRENT_DATE AND model IS NOT NULL
                GROUP BY model ORDER BY n DESC LIMIT 1
                """
            )
        )
        top_model_row = top_model_result.first()
        top_model = top_model_row.model if top_model_row else "—"

        # Total user count
        user_count_result = await session.execute(
            select(func.count()).select_from(User)
        )
        user_count = user_count_result.scalar_one()

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "today_cost": today_cost,
        "today_requests": today_requests,
        "today_error_rate": round(today_error_rate, 1),
        "top_model": top_model,
        "user_count": user_count,
    })
    return request.app.state.templates.TemplateResponse(request, "overview.html", ctx)


# ===========================================================================
# Users
# ===========================================================================


@router.get("/users")
async def users_list(
    request: Request,
    page: int = 1,
    size: int = _PAGE_SIZE_DEFAULT,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Paginated user list."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    size = min(size, _PAGE_SIZE_MAX)

    async with request.app.state.db_session_factory() as session:
        total_result = await session.execute(
            select(func.count()).select_from(User)
        )
        total = int(total_result.scalar_one())

        pg = _paginate(total, page, size)

        result = await session.execute(
            select(User)
            .order_by(User.created_at.desc())
            .limit(pg["size"])
            .offset(pg["offset"])
        )
        users = result.scalars().all()

    ctx = _base_context(request, secret=secret)
    ctx.update({"users": users, **pg})
    return request.app.state.templates.TemplateResponse(request, "users/list.html", ctx)


@router.get("/users/new")
async def users_new_form(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """New-user creation form."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    csrf_token = _csrf_token(request, secret=secret)
    ctx = _base_context(request, secret=secret)
    ctx["csrf_token"] = csrf_token
    return request.app.state.templates.TemplateResponse(request, "users/new.html", ctx)


@router.post("/users")
async def users_create(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Create a new user and render the one-time token display."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    email = str(form.get("email", "")).lower().strip()
    password = str(form.get("password", ""))
    cap_str = str(form.get("monthly_usd_cap", "10.00"))
    is_admin_flag = "is_admin" in form

    if not email or not password:
        return _redirect(
            "/dashboard/users/new",
            flash_msg="Email and password are required.",
            flash_kind="error",
            secret=secret,
        )
    if len(password) < 12:
        return _redirect(
            "/dashboard/users/new",
            flash_msg="Password must be at least 12 characters.",
            flash_kind="error",
            secret=secret,
        )

    try:
        monthly_usd_cap = Decimal(cap_str)
    except Exception:
        monthly_usd_cap = Decimal("10.00")

    pw_hash = hash_password(password)

    async with request.app.state.db_session_factory() as session:
        # Check for duplicate
        existing = await session.execute(
            select(User).where(User.email == email)
        )
        if existing.scalar_one_or_none() is not None:
            await session.rollback()
            return _redirect(
                "/dashboard/users/new",
                flash_msg=f"Email {email} is already registered.",
                flash_kind="error",
                secret=secret,
            )

        new_user = User(
            email=email,
            password_hash=pw_hash,
            monthly_usd_cap=monthly_usd_cap,
            is_active=True,
            is_admin=is_admin_flag,
        )
        session.add(new_user)
        await session.flush()

        raw_refresh = generate_refresh_token()
        token_row = RefreshToken(
            user_id=new_user.id,
            token_hash=hash_refresh_token(raw_refresh),
            expires_at=refresh_token_expiry(settings),
        )
        session.add(token_row)

        access_token, _ = create_access_token(new_user.id, settings)
        user_id_str = str(new_user.id)
        await session.commit()

    logger.info("dashboard.user_created", admin_user_id=str(admin[0].id), new_user_id=user_id_str)

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "new_user_email": email,
        "access_token": access_token,
        "refresh_token": raw_refresh,
    })
    return request.app.state.templates.TemplateResponse(request, "users/tokens_once.html", ctx)


@router.get("/users/{user_id}")
async def user_detail(
    request: Request,
    user_id: UUID,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """User detail page."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    async with request.app.state.db_session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            return _redirect("/dashboard/users", flash_msg="User not found.", flash_kind="error", secret=secret)

        # Monthly spend
        period_start = func.date_trunc("month", func.now())
        spent_result = await session.execute(
            select(func.coalesce(func.sum(RequestLog.cost_usd), 0))
            .where(
                RequestLog.user_id == user_id,
                RequestLog.created_at >= period_start,
            )
        )
        spent_usd = float(spent_result.scalar_one() or 0)

        # Request count
        count_result = await session.execute(
            select(func.count()).select_from(RequestLog).where(RequestLog.user_id == user_id)
        )
        request_count = int(count_result.scalar_one())

        # Recent log rows (last 20)
        recent_result = await session.execute(
            select(RequestLog)
            .where(RequestLog.user_id == user_id)
            .order_by(RequestLog.created_at.desc())
            .limit(20)
        )
        recent_logs = recent_result.scalars().all()

        # API tokens for this user
        tokens_result = await session.execute(
            select(ApiToken)
            .where(ApiToken.user_id == user_id)
            .order_by(ApiToken.created_at.desc())
        )
        api_tokens = tokens_result.scalars().all()

        # Per-token model scope rows so the template can show which
        # specific models each restricted token may use.
        token_ids = [t.id for t in api_tokens]
        token_models: dict[UUID, list[str]] = {tid: [] for tid in token_ids}
        if token_ids:
            scope_rows = await session.execute(
                select(ApiTokenModel.token_id, ApiTokenModel.model)
                .where(ApiTokenModel.token_id.in_(token_ids))
                .order_by(ApiTokenModel.model)
            )
            for tid, model_id in scope_rows.all():
                token_models.setdefault(tid, []).append(model_id)

        # Available pricing rows for the new-token form. Only chat
        # (messages) models — the per-token scope feature does not
        # restrict embeddings, so showing embedding rows here would
        # confuse the operator.
        pricing_result = await session.execute(
            select(ModelPricing)
            .where(
                ModelPricing.disabled_at.is_(None),
                ModelPricing.is_allowed.is_(True),
                ModelPricing.endpoint_kind == "messages",
            )
            .order_by(ModelPricing.model)
        )
        available_models = pricing_result.scalars().all()

    csrf_token = _csrf_token(request, secret=secret)
    ctx = _base_context(request, secret=secret)
    ctx.update({
        "target_user": user,
        "spent_usd": spent_usd,
        "request_count": request_count,
        "recent_logs": recent_logs,
        "api_tokens": api_tokens,
        "token_models": {str(k): v for k, v in token_models.items()},
        "available_models": available_models,
        "csrf_token": csrf_token,
    })
    return request.app.state.templates.TemplateResponse(request, "users/detail.html", ctx)


@router.post("/users/{user_id}/cap")
async def user_update_cap(
    request: Request,
    user_id: UUID,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Update monthly_usd_cap for a user."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    try:
        new_cap = Decimal(str(form.get("monthly_usd_cap", "10.00")))
    except Exception:
        new_cap = Decimal("10.00")

    async with request.app.state.db_session_factory() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(monthly_usd_cap=new_cap)
        )
        await session.commit()

    logger.info("dashboard.user_cap_updated", admin_user_id=str(admin[0].id), user_id=str(user_id))
    return _redirect(f"/dashboard/users/{user_id}", flash_msg="Cap updated.", secret=secret)


@router.post("/users/{user_id}/deactivate")
async def user_deactivate(
    request: Request,
    user_id: UUID,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Deactivate a user and revoke all their sessions + refresh tokens."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    now = _dt.datetime.now(tz=_dt.timezone.utc)
    async with request.app.state.db_session_factory() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(is_active=False)
        )
        # Revoke all refresh tokens
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        # Revoke all dashboard sessions
        await dash_auth.revoke_all_user_sessions(session, user_id=user_id)
        await session.commit()

    logger.info("dashboard.user_deactivated", admin_user_id=str(admin[0].id), user_id=str(user_id))
    return _redirect(f"/dashboard/users/{user_id}", flash_msg="User deactivated.", secret=secret)


@router.post("/users/{user_id}/activate")
async def user_activate(
    request: Request,
    user_id: UUID,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Reactivate a previously deactivated user."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    async with request.app.state.db_session_factory() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(is_active=True)
        )
        await session.commit()

    logger.info("dashboard.user_activated", admin_user_id=str(admin[0].id), user_id=str(user_id))
    return _redirect(f"/dashboard/users/{user_id}", flash_msg="User activated.", secret=secret)


@router.post("/users/{user_id}/regenerate")
async def user_regenerate(
    request: Request,
    user_id: UUID,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Revoke old tokens, issue new access+refresh, show tokens_once page."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    now = _dt.datetime.now(tz=_dt.timezone.utc)
    async with request.app.state.db_session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            return _redirect("/dashboard/users", flash_msg="User not found.", flash_kind="error", secret=secret)

        # Revoke all existing refresh tokens
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

        raw_refresh = generate_refresh_token()
        token_row = RefreshToken(
            user_id=user_id,
            token_hash=hash_refresh_token(raw_refresh),
            expires_at=refresh_token_expiry(settings),
        )
        session.add(token_row)

        access_token, _ = create_access_token(user_id, settings)
        user_email = user.email
        await session.commit()

    logger.info("dashboard.user_tokens_regenerated", admin_user_id=str(admin[0].id), user_id=str(user_id))

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "new_user_email": user_email,
        "access_token": access_token,
        "refresh_token": raw_refresh,
    })
    return request.app.state.templates.TemplateResponse(request, "users/tokens_once.html", ctx)


@router.post("/users/{user_id}/admin")
async def user_toggle_admin(
    request: Request,
    user_id: UUID,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Toggle is_admin for a user."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    async with request.app.state.db_session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            return _redirect("/dashboard/users", flash_msg="User not found.", flash_kind="error", secret=secret)
        new_admin = not user.is_admin
        await session.execute(
            update(User).where(User.id == user_id).values(is_admin=new_admin)
        )
        await session.commit()

    msg = "User is now an admin." if new_admin else "Admin rights removed."
    logger.info("dashboard.user_admin_toggled", admin_user_id=str(admin[0].id), user_id=str(user_id), is_admin=new_admin)
    return _redirect(f"/dashboard/users/{user_id}", flash_msg=msg, secret=secret)


@router.post("/users/{user_id}/tokens")
async def user_create_api_token(
    request: Request,
    user_id: UUID,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Create an API token for a user and show the raw value once."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    raw_form = await request.form()
    # CSRF check uses a flat dict; for the ``models`` multi-value list
    # we keep the original FormData reference so getlist() works.
    if not _check_csrf(dict(raw_form), request=request, secret=secret):
        return _csrf_invalid()

    description = str(raw_form.get("description", "")).strip()
    author = str(raw_form.get("author", "")).strip()
    model_scope = str(raw_form.get("model_scope", "all")).strip()
    selected_models = [
        str(m).strip()
        for m in raw_form.getlist("models")
        if str(m).strip()
    ]
    allow_all_models = model_scope != "custom"

    if not description or not author:
        return _redirect(
            f"/dashboard/users/{user_id}",
            flash_msg="Description and author are required.",
            flash_kind="error",
            secret=secret,
        )

    if not allow_all_models and not selected_models:
        return _redirect(
            f"/dashboard/users/{user_id}",
            flash_msg="Select at least one model when restricting the token.",
            flash_kind="error",
            secret=secret,
        )

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    async with request.app.state.db_session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            return _redirect("/dashboard/users", flash_msg="User not found.", flash_kind="error", secret=secret)

        validated_models: list[str] = []
        if not allow_all_models:
            check = await session.execute(
                select(ModelPricing.model).where(
                    ModelPricing.model.in_(selected_models),
                    ModelPricing.endpoint_kind == "messages",
                )
            )
            known = {r[0] for r in check.all()}
            validated_models = [m for m in selected_models if m in known]
            if not validated_models:
                return _redirect(
                    f"/dashboard/users/{user_id}",
                    flash_msg="Selected models do not exist.",
                    flash_kind="error",
                    secret=secret,
                )

        row = ApiToken(
            user_id=user_id,
            token_hash=token_hash,
            description=description,
            author=author,
            allow_all_models=allow_all_models,
        )
        session.add(row)
        await session.flush()

        if not allow_all_models:
            for model_id in validated_models:
                session.add(ApiTokenModel(token_id=row.id, model=model_id))

        await session.commit()
        token_id = str(row.id)

    logger.info(
        "dashboard.api_token_created",
        admin_user_id=str(admin[0].id),
        user_id=str(user_id),
        token_id=token_id,
        allow_all_models=allow_all_models,
        model_count=len(validated_models),
    )

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "target_user_id": str(user_id),
        "raw_token": raw_token,
        "description": description,
        "author": author,
        "allow_all_models": allow_all_models,
        "scoped_models": validated_models,
    })
    return request.app.state.templates.TemplateResponse(request, "users/api_token_once.html", ctx)


@router.post("/users/{user_id}/tokens/{token_id}/models")
async def user_update_token_models(
    request: Request,
    user_id: UUID,
    token_id: UUID,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Replace the model scope on an existing API token."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    raw_form = await request.form()
    if not _check_csrf(dict(raw_form), request=request, secret=secret):
        return _csrf_invalid()

    model_scope = str(raw_form.get("model_scope", "all")).strip()
    selected_models = [
        str(m).strip()
        for m in raw_form.getlist("models")
        if str(m).strip()
    ]
    allow_all_models = model_scope != "custom"

    if not allow_all_models and not selected_models:
        return _redirect(
            f"/dashboard/users/{user_id}",
            flash_msg="Select at least one model when restricting the token.",
            flash_kind="error",
            secret=secret,
        )

    async with request.app.state.db_session_factory() as session:
        token_row = await session.execute(
            select(ApiToken).where(
                ApiToken.id == token_id, ApiToken.user_id == user_id
            )
        )
        token = token_row.scalar_one_or_none()
        if token is None:
            return _redirect(
                f"/dashboard/users/{user_id}",
                flash_msg="Token not found.",
                flash_kind="error",
                secret=secret,
            )

        validated_models: list[str] = []
        if not allow_all_models:
            check = await session.execute(
                select(ModelPricing.model).where(
                    ModelPricing.model.in_(selected_models),
                    ModelPricing.endpoint_kind == "messages",
                )
            )
            known = {r[0] for r in check.all()}
            validated_models = [m for m in selected_models if m in known]
            if not validated_models:
                return _redirect(
                    f"/dashboard/users/{user_id}",
                    flash_msg="Selected models do not exist.",
                    flash_kind="error",
                    secret=secret,
                )

        token.allow_all_models = allow_all_models
        from sqlalchemy import delete as _delete

        await session.execute(
            _delete(ApiTokenModel).where(ApiTokenModel.token_id == token_id)
        )
        if not allow_all_models:
            for model_id in validated_models:
                session.add(ApiTokenModel(token_id=token_id, model=model_id))
        await session.commit()

    logger.info(
        "dashboard.api_token_models_updated",
        admin_user_id=str(admin[0].id),
        user_id=str(user_id),
        token_id=str(token_id),
        allow_all_models=allow_all_models,
        model_count=len(validated_models),
    )
    msg = (
        "Token now uses all models."
        if allow_all_models
        else f"Token restricted to {len(validated_models)} model(s)."
    )
    return _redirect(f"/dashboard/users/{user_id}", flash_msg=msg, secret=secret)


@router.post("/users/{user_id}/tokens/{token_id}/revoke")
async def user_revoke_api_token(
    request: Request,
    user_id: UUID,
    token_id: UUID,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Revoke (soft-delete) one of a user's API tokens."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    async with request.app.state.db_session_factory() as session:
        await session.execute(
            update(ApiToken)
            .where(ApiToken.id == token_id, ApiToken.user_id == user_id)
            .values(is_active=False)
        )
        await session.commit()

    logger.info("dashboard.api_token_revoked", admin_user_id=str(admin[0].id), user_id=str(user_id), token_id=str(token_id))
    return _redirect(f"/dashboard/users/{user_id}", flash_msg="API token revoked.", secret=secret)


# ===========================================================================
# Models
# ===========================================================================


def _invalidate_pricing_cache(request: Request) -> None:
    """Invalidate the pricing cache on the current worker."""
    cache: PricingCache | None = getattr(request.app.state, "pricing_cache", None)
    if cache is not None:
        cache.invalidate()


@router.get("/models")
async def models_list(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """List all model_pricing rows."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    async with request.app.state.db_session_factory() as session:
        result = await session.execute(
            select(ModelPricing).order_by(ModelPricing.model)
        )
        models = result.scalars().all()

    csrf_token = _csrf_token(request, secret=secret)
    ctx = _base_context(request, secret=secret)
    ctx.update({"models": models, "csrf_token": csrf_token})
    return request.app.state.templates.TemplateResponse(request, "models/list.html", ctx)


@router.get("/models/new")
async def models_new_form(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """New model pricing form."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    csrf_token = _csrf_token(request, secret=secret)
    ctx = _base_context(request, secret=secret)
    ctx.update({
        "csrf_token": csrf_token,
        "action": "/dashboard/models",
        "model_row": None,
    })
    return request.app.state.templates.TemplateResponse(request, "models/form.html", ctx)


@router.post("/models")
async def models_create(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Insert a new model pricing row."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    model_id = str(form.get("model", "")).strip()
    endpoint_kind = str(form.get("endpoint_kind", "messages")).strip()
    is_allowed = "is_allowed" in form

    try:
        input_per_mtoken = Decimal(str(form.get("input_per_mtoken", "0")))
    except Exception:
        input_per_mtoken = Decimal("0")

    output_str = str(form.get("output_per_mtoken", "")).strip()
    output_per_mtoken: Decimal | None = None
    if output_str:
        try:
            output_per_mtoken = Decimal(output_str)
        except Exception:
            output_per_mtoken = None

    cache_read_str = str(form.get("cache_read_per_mtoken", "")).strip()
    cache_read_per_mtoken: Decimal | None = None
    if cache_read_str:
        try:
            cache_read_per_mtoken = Decimal(cache_read_str)
        except Exception:
            cache_read_per_mtoken = None

    cache_write_str = str(form.get("cache_write_per_mtoken", "")).strip()
    cache_write_per_mtoken: Decimal | None = None
    if cache_write_str:
        try:
            cache_write_per_mtoken = Decimal(cache_write_str)
        except Exception:
            cache_write_per_mtoken = None

    notes = str(form.get("notes", "")).strip() or None

    async with request.app.state.db_session_factory() as session:
        row = ModelPricing(
            model=model_id,
            endpoint_kind=endpoint_kind,
            input_per_mtoken=input_per_mtoken,
            output_per_mtoken=output_per_mtoken,
            cache_read_per_mtoken=cache_read_per_mtoken,
            cache_write_per_mtoken=cache_write_per_mtoken,
            is_allowed=is_allowed,
            notes=notes,
        )
        session.add(row)
        await session.commit()

    _invalidate_pricing_cache(request)
    logger.info("dashboard.model_pricing_created", admin_user_id=str(admin[0].id), model=model_id)
    return _redirect("/dashboard/models", flash_msg=f"Model {model_id} created.", secret=secret)


@router.get("/models/{model:path}/edit")
async def models_edit_form(
    request: Request,
    model: str,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Edit model pricing form."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    async with request.app.state.db_session_factory() as session:
        row = await session.get(ModelPricing, model)
        if row is None:
            return _redirect("/dashboard/models", flash_msg="Model not found.", flash_kind="error", secret=secret)

    csrf_token = _csrf_token(request, secret=secret)
    ctx = _base_context(request, secret=secret)
    ctx.update({
        "csrf_token": csrf_token,
        "action": f"/dashboard/models/{model}",
        "model_row": row,
    })
    return request.app.state.templates.TemplateResponse(request, "models/form.html", ctx)


@router.post("/models/{model:path}/delete")
async def models_delete(
    request: Request,
    model: str,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Soft-delete a model pricing row (set disabled_at)."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    now = _dt.datetime.now(tz=_dt.timezone.utc)
    async with request.app.state.db_session_factory() as session:
        await session.execute(
            update(ModelPricing)
            .where(ModelPricing.model == model)
            .values(disabled_at=now, updated_at=now)
        )
        await session.commit()

    _invalidate_pricing_cache(request)
    logger.info("dashboard.model_pricing_deleted", admin_user_id=str(admin[0].id), model=model)
    return _redirect("/dashboard/models", flash_msg=f"Model {model} disabled.", secret=secret)


@router.post("/models/{model:path}")
async def models_update(
    request: Request,
    model: str,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Update a model pricing row."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    endpoint_kind = str(form.get("endpoint_kind", "messages")).strip()
    is_allowed = "is_allowed" in form

    try:
        input_per_mtoken = Decimal(str(form.get("input_per_mtoken", "0")))
    except Exception:
        input_per_mtoken = Decimal("0")

    output_str = str(form.get("output_per_mtoken", "")).strip()
    output_per_mtoken: Decimal | None = None
    if output_str:
        try:
            output_per_mtoken = Decimal(output_str)
        except Exception:
            output_per_mtoken = None

    cache_read_str = str(form.get("cache_read_per_mtoken", "")).strip()
    cache_read_per_mtoken: Decimal | None = None
    if cache_read_str:
        try:
            cache_read_per_mtoken = Decimal(cache_read_str)
        except Exception:
            cache_read_per_mtoken = None

    cache_write_str = str(form.get("cache_write_per_mtoken", "")).strip()
    cache_write_per_mtoken: Decimal | None = None
    if cache_write_str:
        try:
            cache_write_per_mtoken = Decimal(cache_write_str)
        except Exception:
            cache_write_per_mtoken = None

    notes = str(form.get("notes", "")).strip() or None
    now = _dt.datetime.now(tz=_dt.timezone.utc)

    async with request.app.state.db_session_factory() as session:
        await session.execute(
            update(ModelPricing)
            .where(ModelPricing.model == model)
            .values(
                endpoint_kind=endpoint_kind,
                input_per_mtoken=input_per_mtoken,
                output_per_mtoken=output_per_mtoken,
                cache_read_per_mtoken=cache_read_per_mtoken,
                cache_write_per_mtoken=cache_write_per_mtoken,
                is_allowed=is_allowed,
                notes=notes,
                updated_at=now,
            )
        )
        await session.commit()

    _invalidate_pricing_cache(request)
    logger.info("dashboard.model_pricing_updated", admin_user_id=str(admin[0].id), model=model)
    return _redirect("/dashboard/models", flash_msg=f"Model {model} updated.", secret=secret)


# ===========================================================================
# Logs
# ===========================================================================


@router.get("/logs")
async def logs_list(
    request: Request,
    page: int = 1,
    size: int = _PAGE_SIZE_DEFAULT,
    user: str = "",
    endpoint: str = "",
    model: str = "",
    status: str = "",
    from_: str = "",
    to: str = "",
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Paginated request_log list with filters."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    size = min(size, _PAGE_SIZE_MAX)

    # Build dynamic WHERE clauses
    conditions = []
    params: dict[str, Any] = {}

    if user:
        conditions.append(
            "rl.user_id IN (SELECT id FROM users WHERE email ILIKE :user_filter)"
        )
        params["user_filter"] = f"%{user}%"

    if endpoint:
        conditions.append("rl.endpoint = :endpoint_filter")
        params["endpoint_filter"] = endpoint

    if model:
        conditions.append("rl.model ILIKE :model_filter")
        params["model_filter"] = f"%{model}%"

    if status:
        try:
            params["status_filter"] = int(status)
            conditions.append("rl.status_code = :status_filter")
        except ValueError:
            pass

    if from_:
        try:
            params["from_filter"] = _dt.datetime.fromisoformat(from_)
            conditions.append("rl.created_at >= :from_filter")
        except ValueError:
            pass

    if to:
        try:
            params["to_filter"] = _dt.datetime.fromisoformat(to)
            conditions.append("rl.created_at <= :to_filter")
        except ValueError:
            pass

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with request.app.state.db_session_factory() as session:
        count_result = await session.execute(
            text(f"SELECT COUNT(*) FROM request_log rl {where_clause}"),
            params,
        )
        total = int(count_result.scalar_one())

        pg = _paginate(total, page, size)

        rows_result = await session.execute(
            text(
                f"""
                SELECT rl.id, rl.request_id, rl.user_id, rl.endpoint, rl.model,
                       rl.status_code, rl.error_code, rl.latency_ms, rl.cost_usd,
                       rl.tokens_in, rl.tokens_out, rl.created_at,
                       u.email AS user_email
                FROM request_log rl
                LEFT JOIN users u ON u.id = rl.user_id
                {where_clause}
                ORDER BY rl.created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": pg["size"], "offset": pg["offset"]},
        )
        log_rows = rows_result.mappings().all()

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "log_rows": log_rows,
        "filters": {
            "user": user,
            "endpoint": endpoint,
            "model": model,
            "status": status,
            "from_": from_,
            "to": to,
        },
        **pg,
    })
    return request.app.state.templates.TemplateResponse(request, "logs/list.html", ctx)


@router.get("/logs/{log_id:int}")
async def log_detail(
    request: Request,
    log_id: int,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Single request_log row detail."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    async with request.app.state.db_session_factory() as session:
        row = await session.get(RequestLog, log_id)
        if row is None:
            return _redirect("/dashboard/logs", flash_msg="Log entry not found.", flash_kind="error", secret=secret)

        user = await session.get(User, row.user_id) if row.user_id else None

    # Format request body as pretty JSON
    req_body_str = ""
    if row.request_body:
        try:
            req_body_str = json.dumps(row.request_body, indent=2)
        except Exception:
            req_body_str = str(row.request_body)

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "log_row": row,
        "user": user,
        "req_body_str": req_body_str,
    })
    return request.app.state.templates.TemplateResponse(request, "logs/detail.html", ctx)


# ===========================================================================
# Chats — group request_log rows by chat_id and show conversation turns
# ===========================================================================


def _extract_assistant_text(response_body: str | None) -> str:
    """Extract concatenated assistant text from an Anthropic SSE body.

    The streaming response body stored in ``request_log.response_body`` is
    the raw SSE byte stream as the gateway saw it (truncated to
    ``MAX_BODY_BYTES``). Each ``content_block_delta`` event carries a
    JSON payload with ``delta.type == "text_delta"`` and a ``text`` field;
    concatenating those deltas in order yields the assistant message.

    Non-streaming responses store the full JSON body, in which case the
    body has top-level ``content`` blocks. We try the SSE path first and
    fall back to JSON parsing if no deltas are found.

    Returns the empty string when no text could be extracted (the chat
    detail view falls back to showing the raw body in that case).
    """
    if not response_body:
        return ""

    # SSE path — fast scan for ``"text_delta"`` events.
    pieces: list[str] = []
    if '"text_delta"' in response_body or '"delta"' in response_body:
        for line in response_body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            delta = obj.get("delta")
            if isinstance(delta, dict):
                if delta.get("type") == "text_delta":
                    text_part = delta.get("text")
                    if isinstance(text_part, str):
                        pieces.append(text_part)
                # ``thinking_delta`` / ``input_json_delta`` are skipped —
                # the chat view shows assistant output, not internal
                # reasoning or tool args.
        if pieces:
            return "".join(pieces)

    # Non-streaming JSON fallback.
    stripped = response_body.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return ""
        if isinstance(obj, dict):
            content = obj.get("content")
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ):
                        pieces.append(block["text"])
            return "".join(pieces)

    return ""


def _summarize_user_prompt(request_body: dict | None) -> str:
    """Return the last user message in ``request_body.messages`` as text.

    The chat list page wants a short preview of what the user said on
    each turn; we pull the last entry whose role is ``user`` and flatten
    its content blocks to a string. Returns the empty string when there
    is no user message (e.g. system-only seeding).
    """
    if not isinstance(request_body, dict):
        return ""
    messages = request_body.get("messages")
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_part = block.get("text")
                    if isinstance(text_part, str):
                        parts.append(text_part)
                elif block.get("type") == "tool_result":
                    parts.append("[tool_result]")
            return "\n".join(parts)
    return ""


@router.get("/chats")
async def chats_list(
    request: Request,
    page: int = 1,
    size: int = _PAGE_SIZE_DEFAULT,
    user: str = "",
    model: str = "",
    from_: str = "",
    to: str = "",
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """List chats — one row per ``chat_id`` with aggregate stats.

    The grouping is done in SQL because rolling it up in Python would
    pull every request_log row across the result window into memory
    just to bucket them. We sort by the chat's most recent turn
    (``MAX(created_at) DESC``) so active conversations float to the top.
    """
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    size = min(size, _PAGE_SIZE_MAX)

    conditions = ["rl.chat_id IS NOT NULL"]
    params: dict[str, Any] = {}

    if user:
        conditions.append(
            "rl.user_id IN (SELECT id FROM users WHERE email ILIKE :user_filter)"
        )
        params["user_filter"] = f"%{user}%"

    if model:
        conditions.append("rl.model ILIKE :model_filter")
        params["model_filter"] = f"%{model}%"

    if from_:
        try:
            params["from_filter"] = _dt.datetime.fromisoformat(from_)
            conditions.append("rl.created_at >= :from_filter")
        except ValueError:
            pass

    if to:
        try:
            params["to_filter"] = _dt.datetime.fromisoformat(to)
            conditions.append("rl.created_at <= :to_filter")
        except ValueError:
            pass

    where_clause = "WHERE " + " AND ".join(conditions)

    async with request.app.state.db_session_factory() as session:
        count_result = await session.execute(
            text(
                f"""
                SELECT COUNT(*) FROM (
                    SELECT rl.chat_id FROM request_log rl
                    {where_clause}
                    GROUP BY rl.chat_id
                ) t
                """
            ),
            params,
        )
        total = int(count_result.scalar_one())

        pg = _paginate(total, page, size)

        rows_result = await session.execute(
            text(
                f"""
                SELECT
                    rl.chat_id,
                    COUNT(*) AS turn_count,
                    MIN(rl.created_at) AS first_at,
                    MAX(rl.created_at) AS last_at,
                    COALESCE(SUM(rl.cost_usd), 0) AS total_cost,
                    COALESCE(SUM(rl.tokens_in), 0) AS total_tokens_in,
                    COALESCE(SUM(rl.tokens_out), 0) AS total_tokens_out,
                    COALESCE(SUM(rl.cache_read_tokens), 0) AS total_cache_read,
                    COALESCE(SUM(rl.cache_write_tokens), 0) AS total_cache_write,
                    (ARRAY_AGG(DISTINCT rl.model) FILTER (WHERE rl.model IS NOT NULL))[1] AS model,
                    (ARRAY_AGG(DISTINCT rl.user_id) FILTER (WHERE rl.user_id IS NOT NULL))[1] AS user_id,
                    (
                        SELECT u.email FROM users u
                        WHERE u.id = (
                            ARRAY_AGG(DISTINCT rl.user_id) FILTER (WHERE rl.user_id IS NOT NULL)
                        )[1]
                    ) AS user_email,
                    SUM(CASE WHEN rl.status_code >= 400 THEN 1 ELSE 0 END) AS error_count
                FROM request_log rl
                {where_clause}
                GROUP BY rl.chat_id
                ORDER BY MAX(rl.created_at) DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": pg["size"], "offset": pg["offset"]},
        )
        chat_rows = rows_result.mappings().all()

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "chat_rows": chat_rows,
        "filters": {
            "user": user,
            "model": model,
            "from_": from_,
            "to": to,
        },
        **pg,
    })
    return request.app.state.templates.TemplateResponse(request, "chats/list.html", ctx)


@router.get("/chats/{chat_id}")
async def chat_detail(
    request: Request,
    chat_id: str,
    turn: int | None = None,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Show every turn of one chat as a conversation transcript.

    Each ``request_log`` row in the chat is a turn. The active turn
    (selected via ``?turn=<id>`` or the most recent by default) renders
    its full prompt + response, while the sidebar lists all turns with
    relative timestamps and quick stats. The shape mirrors the
    ``log-viewer`` reference under ``D:\\office_work\\ai\\log-viewer``.
    """
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    async with request.app.state.db_session_factory() as session:
        rows_result = await session.execute(
            select(RequestLog)
            .where(RequestLog.chat_id == chat_id)
            .order_by(RequestLog.created_at.asc())
        )
        turns = list(rows_result.scalars().all())

        if not turns:
            return _redirect(
                "/dashboard/chats",
                flash_msg=f"No turns recorded for chat {chat_id}.",
                flash_kind="error",
                secret=secret,
            )

        # Resolve the selected turn. ``turn`` is the row's BIGSERIAL id,
        # not its index — that way deep-links survive insertions of
        # newer rows ahead of the currently-selected one.
        active_turn = turns[-1]
        if turn is not None:
            for t in turns:
                if t.id == turn:
                    active_turn = t
                    break

        # User context — every turn in a chat shares the same user, but
        # tolerate divergence (e.g. an admin replaying a chat) by reading
        # off the active turn.
        chat_user: User | None = None
        if active_turn.user_id is not None:
            chat_user = await session.get(User, active_turn.user_id)

    # Build per-turn metadata for the sidebar without re-querying.
    turn_summaries: list[dict[str, Any]] = []
    total_cost = Decimal(0)
    total_tokens_in = 0
    total_tokens_out = 0
    total_cache_read = 0
    total_cache_write = 0
    for t in turns:
        total_cost += t.cost_usd or Decimal(0)
        total_tokens_in += t.tokens_in or 0
        total_tokens_out += t.tokens_out or 0
        total_cache_read += t.cache_read_tokens or 0
        total_cache_write += t.cache_write_tokens or 0
        turn_summaries.append({
            "id": t.id,
            "request_id": str(t.request_id),
            "created_at": t.created_at,
            "latency_ms": t.latency_ms,
            "status_code": t.status_code,
            "error_code": t.error_code,
            "model": t.model,
            "tokens_in": t.tokens_in,
            "tokens_out": t.tokens_out,
            "cache_read_tokens": t.cache_read_tokens,
            "cache_write_tokens": t.cache_write_tokens,
            "cost_usd": t.cost_usd,
            "preview": (_summarize_user_prompt(t.request_body) or "")[:120],
            "is_active": t.id == active_turn.id,
        })

    # Render the active turn's prompt list + assistant text.
    request_body = active_turn.request_body or {}
    raw_messages = (
        request_body.get("messages") if isinstance(request_body, dict) else None
    )
    if not isinstance(raw_messages, list):
        raw_messages = []

    system_prompt: str | list[Any] | None = None
    if isinstance(request_body, dict):
        system_prompt = request_body.get("system")

    assistant_text = _extract_assistant_text(active_turn.response_body)

    try:
        request_body_pretty = json.dumps(active_turn.request_body, indent=2) if active_turn.request_body else ""
    except Exception:
        request_body_pretty = str(active_turn.request_body or "")

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "chat_id": chat_id,
        "chat_user": chat_user,
        "turns": turn_summaries,
        "active_turn": active_turn,
        "active_messages": raw_messages,
        "active_system_prompt": system_prompt,
        "active_assistant_text": assistant_text,
        "active_request_body_pretty": request_body_pretty,
        "totals": {
            "cost_usd": total_cost,
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
            "cache_read_tokens": total_cache_read,
            "cache_write_tokens": total_cache_write,
            "turn_count": len(turns),
        },
    })
    return request.app.state.templates.TemplateResponse(request, "chats/detail.html", ctx)


# ===========================================================================
# Reports
# ===========================================================================


@router.get("/reports/cost")
async def reports_cost(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    ctx = _base_context(request, secret=secret)
    return request.app.state.templates.TemplateResponse(request, "reports/cost.html", ctx)


@router.get("/reports/cost.json")
async def reports_cost_json(
    request: Request,
    window: str = "7d",
    admin=Depends(dash_auth.require_admin),
) -> JSONResponse:
    async with request.app.state.db_session_factory() as session:
        data = await cost_over_time(session, window=window)
    return JSONResponse(content=to_json(data))


@router.get("/reports/users")
async def reports_users(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    ctx = _base_context(request, secret=secret)
    return request.app.state.templates.TemplateResponse(request, "reports/users.html", ctx)


@router.get("/reports/users.json")
async def reports_users_json(
    request: Request,
    window: str = "30d",
    admin=Depends(dash_auth.require_admin),
) -> JSONResponse:
    async with request.app.state.db_session_factory() as session:
        data = await top_users(session, window=window)
    return JSONResponse(content=to_json(data))


@router.get("/reports/errors")
async def reports_errors(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    ctx = _base_context(request, secret=secret)
    return request.app.state.templates.TemplateResponse(request, "reports/errors.html", ctx)


@router.get("/reports/errors.json")
async def reports_errors_json(
    request: Request,
    window: str = "7d",
    admin=Depends(dash_auth.require_admin),
) -> JSONResponse:
    async with request.app.state.db_session_factory() as session:
        data = await errors_over_time(session, window=window)
    return JSONResponse(content=to_json(data))


@router.get("/reports/latency")
async def reports_latency(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    ctx = _base_context(request, secret=secret)
    return request.app.state.templates.TemplateResponse(request, "reports/latency.html", ctx)


@router.get("/reports/latency.json")
async def reports_latency_json(
    request: Request,
    window: str = "24h",
    admin=Depends(dash_auth.require_admin),
) -> JSONResponse:
    async with request.app.state.db_session_factory() as session:
        data = await latency_percentiles(session, window=window)
    return JSONResponse(content=to_json(data))


# ===========================================================================
# Settings (OpenRouter credentials)
# ===========================================================================

_SETTINGS_KEYS = [SETTING_OPENROUTER_KEY]


def _mask(value: str) -> str:
    """Return a masked representation for display (show first 4 chars only)."""
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return value[:4] + "****"


@router.get("/settings")
async def settings_page(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Render the credentials settings page."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    cred_store: CredentialStore = request.app.state.credential_store
    async with request.app.state.db_session_factory() as session:
        db_values = await cred_store.get_db_values(session)

    # Build per-key status: where the value is coming from (db / env / unset)
    def _source(key: str) -> str:
        if key in db_values:
            return "db"
        if key == SETTING_OPENROUTER_KEY and settings.openrouter_api_key:
            return "env"
        return "unset"

    def _display_value(key: str) -> str:
        if key in db_values:
            return _mask(db_values[key])
        if key == SETTING_OPENROUTER_KEY and settings.openrouter_api_key:
            return _mask(settings.openrouter_api_key.get_secret_value())
        return ""

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "source": {k: _source(k) for k in _SETTINGS_KEYS},
        "display": {k: _display_value(k) for k in _SETTINGS_KEYS},
        "SETTING_OPENROUTER_KEY": SETTING_OPENROUTER_KEY,
    })
    return request.app.state.templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings")
async def settings_save(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Persist credential settings to gateway_settings table."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    user: User = request.state.dashboard_user
    now = _dt.datetime.now(tz=_dt.timezone.utc)

    updates: dict[str, str] = {}
    for key in _SETTINGS_KEYS:
        val = str(form.get(key, "")).strip()
        if val:
            updates[key] = val

    if updates:
        async with request.app.state.db_session_factory() as session:
            for key, val in updates.items():
                existing = await session.get(GatewaySettings, key)
                if existing is None:
                    session.add(GatewaySettings(
                        key=key,
                        value=val,
                        updated_at=now,
                        updated_by_id=user.id,
                    ))
                else:
                    existing.value = val
                    existing.updated_at = now
                    existing.updated_by_id = user.id
            await session.commit()

        cred_store: CredentialStore = request.app.state.credential_store
        cred_store.invalidate()
        logger.info(
            "dashboard.settings_updated",
            admin_user_id=str(user.id),
            keys=list(updates.keys()),
        )

    return _redirect(
        "/dashboard/settings",
        flash_msg="Settings saved." if updates else "No changes — empty fields were ignored.",
        flash_kind="success" if updates else "info",
        secret=secret,
    )


# ===========================================================================
# Server status — operational snapshot (DB sizes, connections, Redis, disk)
# ===========================================================================


@router.get("/server")
async def server_status(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Render the server-status page.

    All panels are rendered in one round-trip so the operator gets a
    consistent snapshot. The page is read-only — no CSRF token, no
    forms — and the queries are cheap (catalog reads + a single
    Redis SCAN per known key prefix).
    """
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    redis_client = request.app.state.redis
    started_monotonic = getattr(request.app.state, "started_monotonic", None)

    async with request.app.state.db_session_factory() as session:
        db_summary = await server_stats.database_size(session)
        tables = await server_stats.table_sizes(session)
        connections = await server_stats.connection_stats(session)
        cache = await server_stats.cache_hit_ratio(session)
        top_rows = await server_stats.top_request_log_rows(session, limit=10)

    # Redis + disk are independent of the DB session — could parallelise
    # with asyncio.gather later, but keeping it sequential keeps the
    # error story simple (one stage fails → we know which one).
    try:
        redis_info = await server_stats.redis_summary(redis_client)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("dashboard.server.redis_failed", error=str(exc))
        redis_info = None

    try:
        disk = await server_stats.host_disk_usage("/")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("dashboard.server.disk_failed", error=str(exc))
        disk = None

    uptime = server_stats.app_uptime(started_monotonic)

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "db_summary": db_summary,
        "tables": tables,
        "connections": connections,
        "cache": cache,
        "top_rows": top_rows,
        "redis_info": redis_info,
        "disk": disk,
        "uptime": uptime,
        "gateway_version": settings.version,
    })
    return request.app.state.templates.TemplateResponse(request, "server.html", ctx)


# ===========================================================================
# Vector DB  (pgvector embeddings management)
# ===========================================================================

from gateway.dashboard.vectordb import EMBED_MODEL, EmbedProviderStatus
from gateway.upstream.pgvector import upsert as _pgvec_upsert, search as _pgvec_search

_COLLECTION_RE = _re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

_VECTORDB_PAGE_SIZE = 25


def _get_embed_status(request: Request) -> EmbedProviderStatus:
    status_obj: EmbedProviderStatus | None = getattr(
        request.app.state, "embed_provider_status", None
    )
    if status_obj is None:
        # Lazily create if the app didn't wire it in lifespan (e.g. tests).
        status_obj = EmbedProviderStatus()
        request.app.state.embed_provider_status = status_obj
    return status_obj


async def _embed_text(text: str, request: Request, session: Any) -> list[float]:
    """Embed ``text`` via OpenRouter and return the vector.

    Raises :exc:`RuntimeError` with a user-safe message on any failure.
    """
    from gateway.credential_store import CredentialMissing, SETTING_OPENROUTER_KEY
    from gateway.upstream.openrouter import call_embeddings
    import httpx as _httpx

    settings = get_settings()
    cred_store: CredentialStore = request.app.state.credential_store
    client = request.app.state.openrouter_client

    try:
        api_key = await cred_store.resolve(SETTING_OPENROUTER_KEY, session)
    except CredentialMissing:
        raise RuntimeError("Embedding provider not configured — OpenRouter API key missing.")

    try:
        resp, parsed = await call_embeddings(
            client,
            api_key=api_key,
            base_url=settings.openrouter_base_url,
            model=EMBED_MODEL,
            inputs=[text],
        )
    except _httpx.HTTPError as exc:
        raise RuntimeError(f"Embedding provider unreachable: {type(exc).__name__}") from exc

    if resp.status_code >= 400:
        raise RuntimeError(f"Embedding provider returned HTTP {resp.status_code}.")

    if parsed is None or "data" not in parsed or not parsed["data"]:
        raise RuntimeError("Embedding provider returned an unexpected response shape.")

    vector = parsed["data"][0].get("embedding")
    if not isinstance(vector, list) or len(vector) != 1536:
        raise RuntimeError("Embedding provider returned a vector with unexpected dimension.")

    return vector


@router.get("/vectordb")
async def vectordb_list(
    request: Request,
    page: int = 1,
    collection: str = "",
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """List embeddings with optional collection filter."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    size = _VECTORDB_PAGE_SIZE

    async with request.app.state.db_session_factory() as session:
        stmt_base = select(Embedding)
        count_base = select(func.count()).select_from(Embedding)
        if collection:
            stmt_base = stmt_base.where(Embedding.collection == collection)
            count_base = count_base.where(Embedding.collection == collection)

        total_r = await session.execute(count_base)
        total = int(total_r.scalar_one())
        pg = _paginate(total, page, size)

        rows_r = await session.execute(
            stmt_base.order_by(Embedding.id.desc()).limit(pg["size"]).offset(pg["offset"])
        )
        rows = rows_r.scalars().all()

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "rows": rows,
        "collection_filter": collection,
        **pg,
    })
    return request.app.state.templates.TemplateResponse(request, "vectordb/list.html", ctx)


@router.get("/vectordb/new")
async def vectordb_new_form(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """New embedding form."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    csrf_token = _csrf_token(request, secret=secret)
    ctx = _base_context(request, secret=secret)
    ctx.update({
        "csrf_token": csrf_token,
        "action": "/dashboard/vectordb",
        "row": None,
        "form_values": {},
    })
    return request.app.state.templates.TemplateResponse(request, "vectordb/form.html", ctx)


@router.get("/vectordb/search")
async def vectordb_search_form(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Render the search form (no results yet)."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()
    csrf_token = _csrf_token(request, secret=secret)
    ctx = _base_context(request, secret=secret)
    ctx.update({
        "csrf_token": csrf_token,
        "results": None,
        "form_values": {},
        "provider_ok": True,
        "provider_reason": None,
    })
    return request.app.state.templates.TemplateResponse(request, "vectordb/search.html", ctx)


@router.post("/vectordb/search")
async def vectordb_search_post(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Execute a similarity search; HTMX swaps only the results div."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    collection = str(form.get("collection", "")).strip()
    query_text = str(form.get("query_text", "")).strip()
    limit_str = str(form.get("limit", "10")).strip()
    threshold_str = str(form.get("score_threshold", "")).strip()

    try:
        limit = max(1, min(50, int(limit_str)))
    except ValueError:
        limit = 10

    score_threshold: float | None = None
    if threshold_str:
        try:
            score_threshold = float(threshold_str)
        except ValueError:
            score_threshold = None

    form_values = {
        "collection": collection,
        "query_text": query_text,
        "limit": limit,
        "score_threshold": score_threshold,
    }

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "csrf_token": _csrf_token(request, secret=secret),
        "results": None,
        "form_values": form_values,
        "provider_ok": True,
        "provider_reason": None,
    })

    if not collection or not _COLLECTION_RE.match(collection):
        ctx["provider_ok"] = False
        ctx["provider_reason"] = None
        ctx.update({"flash": {"msg": "Collection name is required and must match ^[A-Za-z0-9_-]{1,64}$.", "kind": "error"}})
        return request.app.state.templates.TemplateResponse(
            request, "vectordb/search.html", ctx, status_code=400
        )

    if not query_text:
        ctx.update({"flash": {"msg": "Query text is required.", "kind": "error"}})
        return request.app.state.templates.TemplateResponse(
            request, "vectordb/search.html", ctx, status_code=400
        )

    embed_status = _get_embed_status(request)

    async with request.app.state.db_session_factory() as session:
        provider_ok, provider_reason = await embed_status.check(
            client=request.app.state.openrouter_client,
            cred_store=request.app.state.credential_store,
            pricing_cache=request.app.state.pricing_cache,
            session=session,
            base_url=settings.openrouter_base_url,
        )

        ctx["provider_ok"] = provider_ok
        ctx["provider_reason"] = provider_reason

        if not provider_ok:
            ctx.update({"flash": {
                "msg": f"Embeddings provider unavailable — cannot search until OpenRouter responds with a valid response for {EMBED_MODEL}. Reason: {provider_reason}",
                "kind": "error",
            }})
            return request.app.state.templates.TemplateResponse(
                request, "vectordb/search.html", ctx, status_code=503
            )

        try:
            vector = await _embed_text(query_text, request, session)
        except RuntimeError as exc:
            embed_status.invalidate()
            ctx.update({"flash": {"msg": str(exc), "kind": "error"}})
            return request.app.state.templates.TemplateResponse(
                request, "vectordb/search.html", ctx, status_code=503
            )

        result = await _pgvec_search(
            session,
            collection=collection,
            vector=vector,
            limit=limit,
            filter=None,
            with_payload=True,
            score_threshold=score_threshold,
        )

    logger.info(
        "dashboard.vectordb.search",
        admin_user_id=str(request.state.dashboard_user.id),
        collection=collection,
        result_count=len(result.get("result", [])),
    )

    ctx["results"] = result.get("result", [])
    # HTMX partial: if the request has HX-Request header, return only the
    # results fragment; otherwise render the full page.
    is_htmx = request.headers.get("HX-Request") == "true"
    template = "vectordb/search_results.html" if is_htmx else "vectordb/search.html"
    return request.app.state.templates.TemplateResponse(request, template, ctx)


@router.post("/vectordb")
async def vectordb_create(
    request: Request,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Create a new embedding row."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    collection = str(form.get("collection", "")).strip()
    point_id = str(form.get("point_id", "")).strip() or str(_uuid.uuid4())
    text_val = str(form.get("text", "")).strip()

    form_values = {"collection": collection, "point_id": point_id, "text": text_val}

    if not collection or not _COLLECTION_RE.match(collection):
        ctx = _base_context(request, secret=secret)
        ctx.update({
            "csrf_token": _csrf_token(request, secret=secret),
            "action": "/dashboard/vectordb",
            "row": None,
            "form_values": form_values,
            "flash": {"msg": "Collection name is required and must match ^[A-Za-z0-9_-]{1,64}$.", "kind": "error"},
        })
        return request.app.state.templates.TemplateResponse(
            request, "vectordb/form.html", ctx, status_code=400
        )

    if not text_val:
        ctx = _base_context(request, secret=secret)
        ctx.update({
            "csrf_token": _csrf_token(request, secret=secret),
            "action": "/dashboard/vectordb",
            "row": None,
            "form_values": form_values,
            "flash": {"msg": "Text is required.", "kind": "error"},
        })
        return request.app.state.templates.TemplateResponse(
            request, "vectordb/form.html", ctx, status_code=400
        )

    embed_status = _get_embed_status(request)

    async with request.app.state.db_session_factory() as session:
        provider_ok, provider_reason = await embed_status.check(
            client=request.app.state.openrouter_client,
            cred_store=request.app.state.credential_store,
            pricing_cache=request.app.state.pricing_cache,
            session=session,
            base_url=settings.openrouter_base_url,
        )

        if not provider_ok:
            ctx = _base_context(request, secret=secret)
            ctx.update({
                "csrf_token": _csrf_token(request, secret=secret),
                "action": "/dashboard/vectordb",
                "row": None,
                "form_values": form_values,
                "flash": {
                    "msg": f"Embeddings provider unavailable — cannot create until OpenRouter responds with a valid response for {EMBED_MODEL}. Reason: {provider_reason}",
                    "kind": "error",
                },
            })
            return request.app.state.templates.TemplateResponse(
                request, "vectordb/form.html", ctx, status_code=503
            )

        try:
            vector = await _embed_text(text_val, request, session)
        except RuntimeError as exc:
            embed_status.invalidate()
            ctx = _base_context(request, secret=secret)
            ctx.update({
                "csrf_token": _csrf_token(request, secret=secret),
                "action": "/dashboard/vectordb",
                "row": None,
                "form_values": form_values,
                "flash": {"msg": str(exc), "kind": "error"},
            })
            return request.app.state.templates.TemplateResponse(
                request, "vectordb/form.html", ctx, status_code=503
            )

        await _pgvec_upsert(
            session,
            collection=collection,
            points=[{"id": point_id, "vector": vector, "payload": {"text": text_val}}],
        )

    logger.info(
        "dashboard.vectordb.created",
        admin_user_id=str(request.state.dashboard_user.id),
        collection=collection,
        point_id=point_id,
    )
    return _redirect(
        "/dashboard/vectordb",
        flash_msg=f"Embedding created (point_id={point_id}).",
        flash_kind="success",
        secret=secret,
    )


@router.get("/vectordb/{embed_id:int}/edit")
async def vectordb_edit_form(
    request: Request,
    embed_id: int,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Edit-embedding form."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    async with request.app.state.db_session_factory() as session:
        row = await session.get(Embedding, embed_id)
        if row is None:
            return _redirect(
                "/dashboard/vectordb",
                flash_msg="Embedding not found.",
                flash_kind="error",
                secret=secret,
            )

    csrf_token = _csrf_token(request, secret=secret)
    ctx = _base_context(request, secret=secret)
    ctx.update({
        "csrf_token": csrf_token,
        "action": f"/dashboard/vectordb/{embed_id}",
        "row": row,
        "form_values": {
            "collection": row.collection,
            "point_id": row.point_id,
            "text": row.payload.get("text", "") if row.payload else "",
        },
    })
    return request.app.state.templates.TemplateResponse(request, "vectordb/form.html", ctx)


@router.post("/vectordb/{embed_id:int}")
async def vectordb_update(
    request: Request,
    embed_id: int,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Update (re-embed) an existing embedding row."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    collection = str(form.get("collection", "")).strip()
    point_id = str(form.get("point_id", "")).strip()
    text_val = str(form.get("text", "")).strip()

    form_values = {"collection": collection, "point_id": point_id, "text": text_val}

    if not collection or not _COLLECTION_RE.match(collection):
        ctx = _base_context(request, secret=secret)
        ctx.update({
            "csrf_token": _csrf_token(request, secret=secret),
            "action": f"/dashboard/vectordb/{embed_id}",
            "row": None,
            "form_values": form_values,
            "flash": {"msg": "Collection name must match ^[A-Za-z0-9_-]{1,64}$.", "kind": "error"},
        })
        return request.app.state.templates.TemplateResponse(
            request, "vectordb/form.html", ctx, status_code=400
        )

    if not text_val:
        ctx = _base_context(request, secret=secret)
        ctx.update({
            "csrf_token": _csrf_token(request, secret=secret),
            "action": f"/dashboard/vectordb/{embed_id}",
            "row": None,
            "form_values": form_values,
            "flash": {"msg": "Text is required.", "kind": "error"},
        })
        return request.app.state.templates.TemplateResponse(
            request, "vectordb/form.html", ctx, status_code=400
        )

    embed_status = _get_embed_status(request)

    async with request.app.state.db_session_factory() as session:
        row = await session.get(Embedding, embed_id)
        if row is None:
            return _redirect(
                "/dashboard/vectordb",
                flash_msg="Embedding not found.",
                flash_kind="error",
                secret=secret,
            )

        if not point_id:
            point_id = row.point_id

        provider_ok, provider_reason = await embed_status.check(
            client=request.app.state.openrouter_client,
            cred_store=request.app.state.credential_store,
            pricing_cache=request.app.state.pricing_cache,
            session=session,
            base_url=settings.openrouter_base_url,
        )

        if not provider_ok:
            ctx = _base_context(request, secret=secret)
            ctx.update({
                "csrf_token": _csrf_token(request, secret=secret),
                "action": f"/dashboard/vectordb/{embed_id}",
                "row": row,
                "form_values": form_values,
                "flash": {
                    "msg": f"Embeddings provider unavailable — cannot edit until OpenRouter responds with a valid response for {EMBED_MODEL}. Reason: {provider_reason}",
                    "kind": "error",
                },
            })
            return request.app.state.templates.TemplateResponse(
                request, "vectordb/form.html", ctx, status_code=503
            )

        try:
            vector = await _embed_text(text_val, request, session)
        except RuntimeError as exc:
            embed_status.invalidate()
            ctx = _base_context(request, secret=secret)
            ctx.update({
                "csrf_token": _csrf_token(request, secret=secret),
                "action": f"/dashboard/vectordb/{embed_id}",
                "row": row,
                "form_values": form_values,
                "flash": {"msg": str(exc), "kind": "error"},
            })
            return request.app.state.templates.TemplateResponse(
                request, "vectordb/form.html", ctx, status_code=503
            )

        await _pgvec_upsert(
            session,
            collection=collection,
            points=[{"id": point_id, "vector": vector, "payload": {"text": text_val}}],
        )

        # If the primary key row is different from the upsert key (i.e. user
        # changed collection or point_id), remove the old row.
        if row.collection != collection or row.point_id != point_id:
            await session.delete(row)
            await session.commit()

    logger.info(
        "dashboard.vectordb.updated",
        admin_user_id=str(request.state.dashboard_user.id),
        embed_id=embed_id,
        collection=collection,
        point_id=point_id,
    )
    return _redirect(
        "/dashboard/vectordb",
        flash_msg=f"Embedding updated (point_id={point_id}).",
        flash_kind="success",
        secret=secret,
    )


@router.post("/vectordb/{embed_id:int}/delete")
async def vectordb_delete(
    request: Request,
    embed_id: int,
    admin=Depends(dash_auth.require_admin),
) -> Response:
    """Hard-delete an embedding row by primary key."""
    settings = get_settings()
    secret = settings.jwt_secret.get_secret_value()

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    async with request.app.state.db_session_factory() as session:
        row = await session.get(Embedding, embed_id)
        if row is not None:
            point_id = row.point_id
            collection = row.collection
            await session.delete(row)
            await session.commit()
        else:
            point_id = str(embed_id)
            collection = ""

    logger.info(
        "dashboard.vectordb.deleted",
        admin_user_id=str(request.state.dashboard_user.id),
        embed_id=embed_id,
        collection=collection,
        point_id=point_id,
    )
    return _redirect(
        "/dashboard/vectordb",
        flash_msg=f"Embedding {embed_id} deleted.",
        flash_kind="success",
        secret=secret,
    )
