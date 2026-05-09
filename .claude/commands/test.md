---
description: Run the Gateway test suite inside the app container
argument-hint: [path-or-marker]
---

Run pytest in the Compose-launched app container so it has access to test Postgres + Redis.

- No argument → run the full suite: `docker compose run --rm app pytest -v`
- Argument given → pass it through: `docker compose run --rm app pytest -v $ARGUMENTS`

Examples of passable arguments:
- `app/tests/test_auth.py` — single file
- `app/tests/test_messages_stream.py::test_first_byte_under_200ms` — single test
- `-k "auth or refresh"` — keyword filter
- `-m "not slow"` — marker filter

If the test database doesn't exist yet, run migrations against it first:
`docker compose run --rm -e DATABASE_URL=$TEST_DATABASE_URL app alembic upgrade head`.

Never `pip install` packages to make a test pass — fix the dependency in `pyproject.toml` and rebuild the image.
