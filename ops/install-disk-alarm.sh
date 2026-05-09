#!/usr/bin/env bash
# Install the GeoSWMM Gateway disk-usage alarm into root's crontab.
#
# Usage:
#   sudo DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." ./install-disk-alarm.sh
#
# Idempotent: re-running won't add duplicate cron lines. The cron line
# itself references $DISCORD_WEBHOOK by name (from the cron environment)
# so rotating the webhook means editing /etc/crontab, not re-running this
# script.

set -euo pipefail

# ---- Pre-flight ----------------------------------------------------------

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: must run as root (cron lives under root's crontab)." >&2
  exit 1
fi

if [[ -z "${DISCORD_WEBHOOK:-}" ]]; then
  echo "ERROR: DISCORD_WEBHOOK is not set in the environment." >&2
  echo "       Export the webhook URL and re-run, e.g.:" >&2
  echo "       sudo DISCORD_WEBHOOK='https://...' $0" >&2
  exit 1
fi

# Validate the webhook is reachable before we install the cron line.
# Using HEAD (which Discord accepts) avoids posting a stray empty
# message during install.
if ! curl --fail --silent --output /dev/null --max-time 10 -X HEAD "${DISCORD_WEBHOOK}"; then
  echo "WARN: webhook HEAD failed; install will proceed but check the URL." >&2
fi

# ---- Cron line -----------------------------------------------------------

CRON_LINE='*/15 * * * * df /var/lib/docker | awk '\''NR==2 && $5+0>80 {print $0}'\'' | curl -s -X POST -H '\''Content-Type: application/json'\'' -d "@-" $DISCORD_WEBHOOK'

# Read root's crontab; if there's nothing yet, `crontab -l` exits 1 — fall
# back to an empty string so the rest of the script still runs.
EXISTING="$(crontab -l 2>/dev/null || true)"

if grep -Fq -- "${CRON_LINE}" <<<"${EXISTING}"; then
  echo "Cron line already present — skipping install."
else
  # Prepend a DISCORD_WEBHOOK= line so the cron environment carries it.
  # If the variable is already in the crontab (re-run with new URL), we
  # replace it instead of appending a duplicate.
  TMP="$(mktemp)"
  trap 'rm -f "${TMP}"' EXIT
  {
    if grep -q '^DISCORD_WEBHOOK=' <<<"${EXISTING}"; then
      sed "s|^DISCORD_WEBHOOK=.*|DISCORD_WEBHOOK=${DISCORD_WEBHOOK}|" <<<"${EXISTING}"
    else
      echo "DISCORD_WEBHOOK=${DISCORD_WEBHOOK}"
      [[ -n "${EXISTING}" ]] && echo "${EXISTING}"
    fi
    echo "${CRON_LINE}"
  } > "${TMP}"
  crontab "${TMP}"
  echo "Installed cron line; root crontab updated."
fi

# ---- One-shot test -------------------------------------------------------

echo "Running one synthetic alarm to verify webhook reachability..."
TEST_PAYLOAD='{"content":"GeoSWMM Gateway disk-alarm test fired (you can ignore this)."}'
if curl --fail --silent --max-time 10 -X POST \
     -H 'Content-Type: application/json' \
     -d "${TEST_PAYLOAD}" \
     "${DISCORD_WEBHOOK}" >/dev/null
then
  echo "OK: test alarm posted."
else
  echo "ERROR: test alarm failed; the cron line is still installed but check the webhook." >&2
  exit 2
fi

echo "Done. The alarm runs every 15 minutes when /var/lib/docker exceeds 80% usage."
