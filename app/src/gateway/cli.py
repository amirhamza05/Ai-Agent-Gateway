"""``gateway-admin`` — small Click CLI for one-off admin tasks.

Bootstrapped exclusively for Phase D's "promote first admin" flow.
Wired as a console script in ``pyproject.toml``::

    $ docker compose run --rm app gateway-admin promote alice@example.com
    Promoted alice@example.com to admin.

The CLI runs in a one-shot process inside the runtime container — it
opens its own async engine off ``DATABASE_URL`` and disposes of it at
exit. No FastAPI app, no Redis client, no upstream HTTP clients are
created.
"""

from __future__ import annotations

import asyncio
import os
import sys

import click
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def _database_url() -> str:
    """Read ``DATABASE_URL`` directly from the environment.

    We don't go through :class:`gateway.config.Settings` because the CLI
    must boot in containers that don't carry every API-side setting
    (e.g. ``OPENROUTER_API_KEY``, ``QDRANT_API_KEY``). ``DATABASE_URL``
    is the only env var we actually need.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        click.echo("DATABASE_URL is not set.", err=True)
        sys.exit(2)
    return dsn


async def _promote(email: str) -> int:
    """Set ``is_admin=TRUE`` for the user matching ``email``.

    Returns 0 on success, 1 if no row matched.
    """
    normalised = email.lower().strip()
    engine = create_async_engine(_database_url())
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE users SET is_admin = TRUE "
                    "WHERE email = :email"
                ),
                {"email": normalised},
            )
            row_count = result.rowcount or 0
        if row_count == 0:
            click.echo(f"No user with email {email}.", err=True)
            return 1
        click.echo(f"Promoted {email} to admin.")
        return 0
    finally:
        await engine.dispose()


@click.group()
def cli() -> None:
    """gateway-admin — small admin CLI for the GeoSWMM Gateway."""


@cli.command()
@click.argument("email")
def promote(email: str) -> None:
    """Promote the user with EMAIL to admin (sets ``is_admin=TRUE``).

    Exits with status 1 if no user matches, 0 on success.
    """
    rc = asyncio.run(_promote(email))
    sys.exit(rc)


if __name__ == "__main__":  # pragma: no cover
    cli()
