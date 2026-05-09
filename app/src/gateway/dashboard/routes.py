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
import secrets
from dataclasses import asdict
from decimal import Decimal
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
from gateway.credential_store import (
    CredentialStore,
    SETTING_OPENROUTER_KEY,
    SETTING_QDRANT_KEY,
    SETTING_QDRANT_URL,
)
from gateway.db.models import (
    ApiToken,
    DashboardSession,
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
        {"request": request, "flash": _pop_flash(request, secret=settings.jwt_secret.get_secret_value())},
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

    csrf_token = _csrf_token(request, secret=secret)
    ctx = _base_context(request, secret=secret)
    ctx.update({
        "target_user": user,
        "spent_usd": spent_usd,
        "request_count": request_count,
        "recent_logs": recent_logs,
        "api_tokens": api_tokens,
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

    form = dict(await request.form())
    if not _check_csrf(form, request=request, secret=secret):
        return _csrf_invalid()

    description = str(form.get("description", "")).strip()
    author = str(form.get("author", "")).strip()

    if not description or not author:
        return _redirect(
            f"/dashboard/users/{user_id}",
            flash_msg="Description and author are required.",
            flash_kind="error",
            secret=secret,
        )

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    async with request.app.state.db_session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            return _redirect("/dashboard/users", flash_msg="User not found.", flash_kind="error", secret=secret)

        row = ApiToken(
            user_id=user_id,
            token_hash=token_hash,
            description=description,
            author=author,
        )
        session.add(row)
        await session.commit()
        token_id = str(row.id)

    logger.info("dashboard.api_token_created", admin_user_id=str(admin[0].id), user_id=str(user_id), token_id=token_id)

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "target_user_id": str(user_id),
        "raw_token": raw_token,
        "description": description,
        "author": author,
    })
    return request.app.state.templates.TemplateResponse(request, "users/api_token_once.html", ctx)


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

    notes = str(form.get("notes", "")).strip() or None

    async with request.app.state.db_session_factory() as session:
        row = ModelPricing(
            model=model_id,
            endpoint_kind=endpoint_kind,
            input_per_mtoken=input_per_mtoken,
            output_per_mtoken=output_per_mtoken,
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
# Settings (OpenRouter + Qdrant credentials)
# ===========================================================================

_SETTINGS_KEYS = [SETTING_OPENROUTER_KEY, SETTING_QDRANT_URL, SETTING_QDRANT_KEY]


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
        if key == SETTING_QDRANT_URL and settings.qdrant_url:
            return "env"
        if key == SETTING_QDRANT_KEY and settings.qdrant_api_key:
            return "env"
        return "unset"

    def _display_value(key: str) -> str:
        if key in db_values:
            return _mask(db_values[key])
        if key == SETTING_OPENROUTER_KEY and settings.openrouter_api_key:
            return _mask(settings.openrouter_api_key.get_secret_value())
        if key == SETTING_QDRANT_URL and settings.qdrant_url:
            return settings.qdrant_url  # URL is not secret — show it
        if key == SETTING_QDRANT_KEY and settings.qdrant_api_key:
            return _mask(settings.qdrant_api_key.get_secret_value())
        return ""

    ctx = _base_context(request, secret=secret)
    ctx.update({
        "source": {k: _source(k) for k in _SETTINGS_KEYS},
        "display": {k: _display_value(k) for k in _SETTINGS_KEYS},
        "SETTING_OPENROUTER_KEY": SETTING_OPENROUTER_KEY,
        "SETTING_QDRANT_URL": SETTING_QDRANT_URL,
        "SETTING_QDRANT_KEY": SETTING_QDRANT_KEY,
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
