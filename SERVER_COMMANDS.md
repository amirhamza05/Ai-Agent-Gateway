# Server & Docker Cheatsheet — GeoSWMM Gateway

A copy-paste reference for running this gateway on a Linux server. Every command shows **what it does**, **when to use it**, and **expected output** so you can tell if it worked.

> Run all commands from the project root: `cd ~/Ai-Agent-Gateway` first.

---

## Table of contents

1. [First-time setup on a fresh server](#1-first-time-setup-on-a-fresh-server)
2. [Daily operations](#2-daily-operations)
3. [Updating the code on the server](#3-updating-the-code-on-the-server)
4. [Viewing logs](#4-viewing-logs)
5. [Database commands](#5-database-commands)
6. [Admin user management](#6-admin-user-management)
7. [Container management](#7-container-management)
8. [Debugging when something is broken](#8-debugging-when-something-is-broken)
9. [Backups & restore](#9-backups--restore)
10. [Common problems & fixes](#10-common-problems--fixes)
11. [Quick reference card](#11-quick-reference-card)

---

## 1. First-time setup on a fresh server

These steps run **once** when you first install the gateway on a new VPS. After that, you'll only use sections 2-10.

### 1.1. Install Docker

```bash
# Update package list and install Docker + the compose plugin
sudo apt update
sudo apt install -y docker.io docker-compose-plugin

# Verify it works
docker --version
docker compose version
```

Expected: two version numbers print, no errors.

### 1.2. Clone the repo

```bash
cd ~
git clone <your-repo-url> Ai-Agent-Gateway
cd Ai-Agent-Gateway
```

### 1.3. Create the `.env` file

```bash
cp .env.example .env
nano .env
```

Fill in **at minimum**:
- `POSTGRES_PASSWORD` — any long random string
- `DATABASE_URL` — same password embedded in this URL
- `JWT_SECRET` — generate with: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
- `PUBLIC_HOSTNAME` — your domain, e.g. `api.your-domain.com`

You can leave `OPENROUTER_API_KEY`, `QDRANT_URL`, `QDRANT_API_KEY` blank — set them from the dashboard later.

Save and exit nano: `Ctrl+O`, `Enter`, `Ctrl+X`.

### 1.4. Start the database first

```bash
docker compose up -d postgres redis
```

Expected output: `Container gateway-postgres-1  Started`

The `-d` means "detached" (run in background). Wait ~10 seconds for Postgres to become ready.

### 1.5. Run migrations (creates tables + seeds admin user)

```bash
docker compose run --rm app alembic upgrade head
```

Expected: a list of "Running upgrade ... -> ..." lines, ending without errors.

This creates all DB tables AND seeds an admin user `admin@gmail.com` / `password`.

### 1.6. Start the app

```bash
docker compose up -d app caddy
```

### 1.7. Verify it's alive

```bash
curl http://localhost:8000/healthz
```

Expected: `{"ok":true,"version":"0.1.0"}`

### 1.8. First login

Open `http://YOUR_SERVER_IP:8000/dashboard/login` (or `https://YOUR_DOMAIN/dashboard/login` if Caddy is serving TLS).

Login as `admin@gmail.com` / `password`.

Then in the dashboard:
- **Settings** page → enter `OPENROUTER_API_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`.
- **Models** page → add the model pricing rows you want to allow.
- **Users → New User** → create a real admin and deactivate `admin@gmail.com`.

---

## 2. Daily operations

### Check what's running

```bash
docker compose ps
```

Expected — all four services `Up (healthy)`:

```
NAME                 STATUS
gateway-app-1        Up 2 hours (healthy)
gateway-caddy-1      Up 2 hours
gateway-postgres-1   Up 2 hours (healthy)
gateway-redis-1      Up 2 hours (healthy)
```

If any service is `Restarting` or `Exited`, jump to [section 8](#8-debugging-when-something-is-broken).

### Restart everything

```bash
docker compose restart
```

Use after editing `.env` or fixing a stuck container. ~10 seconds of downtime.

### Stop everything (without deleting data)

```bash
docker compose stop
```

The containers stop but the database files survive. Start again with `docker compose up -d`.

### Start everything

```bash
docker compose up -d
```

### Stop AND remove containers (data still survives)

```bash
docker compose down
```

The Postgres data is in a Docker volume named `pgdata`, which is **not** deleted by `down`. Only `down -v` deletes data — don't run that on production.

---

## 3. Updating the code on the server

Whenever I push a fix and you need to deploy it:

```bash
# 1. Pull the new code
cd ~/Ai-Agent-Gateway
git pull

# 2. Rebuild the app image (only the `app` service has source code in it)
docker compose build app

# 3. Run any new database migrations
docker compose run --rm app alembic upgrade head

# 4. Restart the app with the new image
docker compose up -d app
```

If step 3 says "Already at head", there were no DB changes — that's fine.

> **Important:** `git pull` alone does NOT update the running container. The container runs a Docker image baked at build time. You must `docker compose build app` to bake new code into a new image, then `up -d app` to swap the container.

---

## 4. Viewing logs

### Watch live logs (most common)

```bash
docker compose logs app -f
```

`-f` means "follow" — new lines appear as they happen. Press `Ctrl+C` to stop watching (this does NOT stop the app, only your terminal's view).

### Last 100 lines (no follow)

```bash
docker compose logs app --tail 100
```

### Logs from all services at once

```bash
docker compose logs -f
```

### Just errors / tracebacks

```bash
docker compose logs app --tail 500 | grep -E "Traceback|Error|ERROR" -A 10
```

### Only Postgres logs

```bash
docker compose logs postgres -f
```

### Pretty-print structured JSON logs

The app emits one JSON line per request. To make them human-readable:

```bash
# Install jq once: sudo apt install -y jq
docker compose logs -f app | jq -R 'fromjson? // .'
```

### Filter logs by request_id

When the add-in shows a `request_id` and you want the matching gateway log line:

```bash
docker compose logs app | jq -R 'fromjson? | select(.request_id == "abcd-1234-...")'
```

### Logs from the last 5 minutes only

```bash
docker compose logs app --since 5m
```

---

## 5. Database commands

### Open a SQL shell

```bash
docker compose exec postgres psql -U gateway -d gateway
```

You're now in `psql`. Useful commands inside:

```sql
\dt                    -- list all tables
\d request_log         -- show columns of one table
SELECT COUNT(*) FROM request_log;
SELECT email, is_admin FROM users;
\q                     -- quit
```

### Run one SQL query without entering the shell

```bash
docker compose exec postgres psql -U gateway -d gateway \
    -c "SELECT email, is_admin, monthly_usd_cap FROM users;"
```

### Run migrations

```bash
docker compose run --rm app alembic upgrade head
```

Use this every time you pull new code. Safe to run when there's nothing to do — it just prints "already at head".

### Show current migration version

```bash
docker compose exec app alembic current
```

Expected: a hash like `a7b8c9d0e1f2 (head)`. If it doesn't say `(head)`, run `alembic upgrade head`.

### Show migration history

```bash
docker compose exec app alembic history
```

### Roll back ONE migration

```bash
docker compose exec app alembic downgrade -1
```

> **Warning:** downgrading can drop tables / columns. Don't do this on production unless you have a backup.

---

## 6. Admin user management

### Promote an existing user to admin

```bash
docker compose run --rm app gateway-admin promote teammate@example.com
```

The user must already exist (registered via `/auth/register` or created from the dashboard).

### Reset a user's password (via SQL)

There's no CLI for this — easiest is to delete the user from the dashboard, recreate them with a new password, OR run an in-container Python one-liner:

```bash
docker compose exec app python -c "
import asyncio
from gateway.config import get_settings
from gateway.db.session import create_session_factory, create_engine
from gateway.auth.passwords import hash_password
from sqlalchemy import update
from gateway.db.models import User

async def main():
    s = get_settings()
    engine = create_engine(s.database_url)
    f = create_session_factory(engine)
    async with f() as session:
        await session.execute(
            update(User)
            .where(User.email == 'someone@example.com')
            .values(password_hash=hash_password('NEW_PASSWORD_HERE'))
        )
        await session.commit()
    await engine.dispose()
    print('done')

asyncio.run(main())
"
```

Replace `someone@example.com` and `NEW_PASSWORD_HERE`. Password must be at least 12 characters.

### List all users

```bash
docker compose exec postgres psql -U gateway -d gateway \
    -c "SELECT email, is_admin, is_active, monthly_usd_cap FROM users;"
```

---

## 7. Container management

### Open a shell inside the running app container

```bash
docker compose exec app bash
```

You're now inside the container. Type `exit` to leave. Useful for inspecting files at `/app/src/...` or running ad-hoc Python.

### See real-time CPU / memory usage

```bash
docker stats
```

Press `Ctrl+C` to stop. Watch for the `app` container climbing past 800 MB — that's near the 1 GB limit set in `docker-compose.yml`.

### Disk usage by Docker

```bash
docker system df
```

Shows space used by images, containers, volumes, and build cache.

### Clean up old/unused images

```bash
docker image prune -f          # safe: only removes dangling images
docker system prune -f         # removes unused images, networks, build cache
```

> **Don't** run `docker system prune --volumes` — that would delete the `pgdata` volume and wipe your database.

### See exactly which image a container is running

```bash
docker compose images
```

---

## 8. Debugging when something is broken

### Page shows "Internal Server Error"

```bash
# 1. Watch logs
docker compose logs app -f

# 2. In a browser, refresh the broken page
# 3. Copy the Traceback that appears in the log

# Or: grab errors from the last 200 lines after the fact
docker compose logs app --tail 200 | grep -E "Traceback|Error" -A 15
```

### App container won't start (keeps restarting)

```bash
# See why it's failing
docker compose logs app --tail 50

# Common causes:
# - Postgres not ready yet  → wait 30 sec, try again
# - Bad DATABASE_URL in .env → check the password
# - Migration not at head   → docker compose run --rm app alembic upgrade head
```

### Make logs verbose for one debugging session

Edit `.env`:

```
LOG_LEVEL=DEBUG
LOG_FORMAT=console
```

Then:

```bash
docker compose restart app
docker compose logs app -f
```

Set them back to `INFO` / `json` for production.

### Verify the running container has the latest code

```bash
# Check the migration head matches what's in source
docker compose exec app alembic heads

# Check a specific file is present in the image
docker compose exec app ls -la /app/src/gateway/dashboard/templates/logs/
```

### Test connectivity from inside the app to Postgres

```bash
docker compose exec app python -c "
import asyncpg, asyncio
async def t():
    conn = await asyncpg.connect('postgresql://gateway:YOUR_PASSWORD@postgres:5432/gateway')
    print(await conn.fetchval('SELECT 1'))
    await conn.close()
asyncio.run(t())
"
```

Replace `YOUR_PASSWORD`. Should print `1`.

### Check Caddy / TLS

```bash
docker compose logs caddy --tail 50
curl -I https://YOUR_DOMAIN/healthz
```

Caddy auto-provisions Let's Encrypt certs the first time it starts, so the first request can take 30-60 seconds.

---

## 9. Backups & restore

### Take a backup right now

```bash
docker compose exec -T postgres \
    pg_dump -U gateway --format=custom gateway \
    > "backup_$(date +%F).dump"

ls -lh backup_*.dump
```

The file lands in your current directory. Copy it off the server immediately:

```bash
# From your laptop:
scp user@YOUR_SERVER:~/Ai-Agent-Gateway/backup_2026-05-09.dump .
```

### Restore a backup on a fresh server

```bash
# 1. Make sure Postgres is up but the app is NOT running (the app would
#    interfere with mid-restore writes).
docker compose up -d postgres
docker compose stop app

# 2. Restore
docker compose exec -T postgres pg_restore \
    -U gateway -d gateway \
    --clean --if-exists \
    < backup_2026-05-09.dump

# 3. Bring the app back
docker compose up -d app
```

`--clean` drops existing tables before recreating them, so the restore is idempotent.

### Schedule a daily backup (cron)

```bash
sudo crontab -e
```

Add this line (runs every day at 03:00):

```
0 3 * * * cd /root/Ai-Agent-Gateway && docker compose exec -T postgres pg_dump -U gateway --format=custom gateway > /root/backups/backup_$(date +\%F).dump 2>&1
```

Make the directory first: `mkdir -p /root/backups`.

---

## 10. Common problems & fixes

### "Internal Server Error" on a dashboard page

→ Stale image. Rebuild and restart:
```bash
git pull
docker compose build app
docker compose run --rm app alembic upgrade head
docker compose up -d app
```

### "Cannot connect to Postgres"

→ Postgres container isn't healthy yet. Check:
```bash
docker compose ps
docker compose logs postgres --tail 30
```

If it shows `password authentication failed`, the `POSTGRES_PASSWORD` in `.env` doesn't match the one Postgres was initialised with. To reset (⚠️ deletes all data):
```bash
docker compose down -v
docker compose up -d postgres
docker compose run --rm app alembic upgrade head
```

### "Port 8000 already in use"

→ Something else on the host is using port 8000. Find and stop it:
```bash
sudo lsof -i :8000
# or
sudo ss -tulpn | grep 8000
```

### "Disk full"

→ Docker logs piling up. Check usage:
```bash
docker system df
sudo du -sh /var/lib/docker/containers/*/
```

If logs are the culprit, the daemon log cap (50 MB × 3 files per container) isn't set yet. Apply it:
```bash
sudo cp ops/docker-daemon.json /etc/docker/daemon.json
sudo systemctl restart docker
```

### "TemplateNotFound" or missing files in container

→ The image was built before the file existed in source, OR the file is in `.gitignore` and was never committed. Verify with:
```bash
docker compose exec app ls /app/src/gateway/dashboard/templates/
git ls-files app/src/gateway/dashboard/templates/
```

If a file is missing from `git ls-files`, check `.gitignore` and add it explicitly.

### "Pricing table empty — every model rejected"

→ The `model_pricing` table has no rows. Sign in to `/dashboard/models` and add at least one model row, OR insert via SQL:
```bash
docker compose exec postgres psql -U gateway -d gateway -c "
INSERT INTO model_pricing (model, endpoint_kind, input_per_mtoken, output_per_mtoken, is_allowed)
VALUES ('anthropic/claude-haiku-4.5', 'messages', 1.00, 5.00, true);
"
```

### "Login fails with correct password"

→ The password might be incorrect, OR the user is not active / not admin. Check:
```bash
docker compose exec postgres psql -U gateway -d gateway \
    -c "SELECT email, is_active, is_admin FROM users WHERE email='admin@gmail.com';"
```

If `is_active=f` or `is_admin=f`, fix it:
```bash
docker compose exec postgres psql -U gateway -d gateway \
    -c "UPDATE users SET is_active=true, is_admin=true WHERE email='admin@gmail.com';"
```

---

## 11. Quick reference card

The 10 commands you'll use most:

```bash
# Status
docker compose ps                            # what's running

# Logs
docker compose logs app -f                   # watch live
docker compose logs app --tail 100           # last 100 lines

# Restart
docker compose restart app                   # restart just the app
docker compose restart                       # restart everything

# Update workflow
git pull
docker compose build app
docker compose run --rm app alembic upgrade head
docker compose up -d app

# Database
docker compose exec postgres psql -U gateway -d gateway     # SQL shell
docker compose run --rm app alembic upgrade head            # run migrations

# Backup
docker compose exec -T postgres pg_dump -U gateway --format=custom gateway > backup_$(date +%F).dump
```

---

## Glossary

- **`docker compose ps`** — list project containers
- **`docker compose up -d <service>`** — start service in background
- **`docker compose down`** — stop AND remove all containers (data survives)
- **`docker compose logs <service> -f`** — tail logs
- **`docker compose exec <service> <cmd>`** — run a command in an already-running container
- **`docker compose run --rm <service> <cmd>`** — start a temporary one-shot container, then delete it
- **`-d`** — detached (run in background)
- **`--rm`** — remove the container as soon as the command finishes
- **`-T`** — disable terminal allocation (needed when piping in/out, e.g. `< backup.dump`)
- **`-f` (in `up`)** — follow / stream logs. (in `prune` it means force / no prompt)
- **Volume** — Docker-managed disk for persistent data (`pgdata`, `redisdata`). Survives container deletion.
- **Image** — the read-only "template" a container is created from. Built by `docker compose build`.
- **Container** — a running instance of an image.
