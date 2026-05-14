---
description: Start the local Gateway dev stack (Postgres + Redis + app) via Docker Compose
---

Start the GeoSWMM Gateway dev stack.

Steps:
1. Verify `.env` exists. If not, instruct the user to copy `.env.example` to `.env` and fill in `OPENROUTER_API_KEY`, `JWT_SECRET`, `POSTGRES_PASSWORD`, then stop.
2. Start dependencies first so the app's wait-for-db is fast: `docker compose up -d postgres redis`.
3. Run pending migrations: `docker compose run --rm app alembic upgrade head`.
4. Start the app in the foreground: `docker compose up app`.
5. Print the local URL the user can hit: `http://localhost:8000/healthz`.

If any step fails, surface the docker error directly and stop. Don't retry blindly.
