# Operator guide — GeoSWMM Gateway VPS

These files cover the host-level safety nets from §8 of
[GatewayServerPlan.md](../GatewayServerPlan.md) and the trial-end backup
hookpoint from §15. None of this is Python or app code — it's the bits
that live on the Ubuntu 24.04 VPS outside the Compose stack.

| File | What it does |
|---|---|
| `docker-daemon.json` | Docker log driver config (50 MB × 3 files per container). |
| `disk-alarm.cron` | Cron snippet that posts to Discord when `/var/lib/docker` > 80%. |
| `install-disk-alarm.sh` | Idempotent installer for the cron line; tests the webhook once. |

## Apply the Docker log cap

Without this, a chatty container (the app, mostly) can fill the 20 GB
disk on its own — `docker logs` defaults to unbounded JSON files in
`/var/lib/docker/containers/<id>/`.

```bash
sudo cp ops/docker-daemon.json /etc/docker/daemon.json
sudo systemctl restart docker
```

The restart bounces every container on the host. Schedule it during a
maintenance window or do it once before the first real users arrive.

Verify the cap took effect:

```bash
docker info --format '{{json .LoggingDriver}} {{json .DefaultLogDriver}} {{json .ContainerdNamespace}}'
docker inspect --format '{{.HostConfig.LogConfig}}' "$(docker compose ps -q app)"
# Expect: {local map[max-file:3 max-size:50m]}
```

## Install the disk-usage alarm

```bash
sudo DISCORD_WEBHOOK='https://discord.com/api/webhooks/<id>/<token>' \
    ops/install-disk-alarm.sh
```

The script:

1. Verifies it's running as root (cron entries live under root's
   crontab so the cron line can read `df /var/lib/docker` without
   permission errors).
2. Checks `$DISCORD_WEBHOOK` is set and the URL is reachable.
3. Appends the cron line from `disk-alarm.cron` to root's crontab if
   it isn't there yet, plus a `DISCORD_WEBHOOK=` line so cron has the
   variable available.
4. Posts one synthetic alarm to verify the webhook works end-to-end.

It's idempotent — re-running with a new webhook URL replaces the
existing `DISCORD_WEBHOOK=` line instead of appending a duplicate.

To uninstall:

```bash
sudo crontab -e   # delete the */15 * * * * line and the DISCORD_WEBHOOK= line
```

## Postgres backups (P7 hookpoint)

Documented now even though P7 lands later. Run from the host (not from
inside any container) so the dump file lands directly on the host
filesystem and is easy to ship to Backblaze / S3:

```bash
docker compose exec -T postgres \
    pg_dump -U gateway --format=custom gateway \
    > "backup_$(date +%F).dump"
```

To restore on a fresh VPS:

```bash
docker compose up -d postgres
docker compose exec -T postgres pg_restore -U gateway -d gateway \
    --clean --if-exists \
    < backup_2026-05-09.dump
```

`--format=custom` is preferred over `--format=plain` because it's
compressed and supports parallel restore on big DBs (not relevant for
the trial month, but worth keeping consistent with the P7 plan).

## Reading structured logs

The app emits one JSON line per request via `structlog`. To follow
them live:

```bash
# Pretty-print (requires jq):
docker compose logs -f app | jq -R 'fromjson? // .'

# Filter by request_id (correlate add-in trace → gateway log → request_log row):
docker compose logs app | jq -R 'fromjson? | select(.request_id == "abcd-...")'

# Show only 4xx/5xx:
docker compose logs app | jq -R 'fromjson? | select(.status_code >= 400)'
```

For deeper queries (sum cost per user this week, find the slowest
endpoints, etc.) hit the `request_log` table directly:

```bash
docker compose exec postgres psql -U gateway -d gateway
```

## Health check

```bash
curl -fsS https://api.your-domain.com/healthz
# {"ok": true, "version": "0.1.0"}
```

The Compose `app` service has its own healthcheck that hits the same
endpoint over the internal network — Caddy waits for it before routing.
