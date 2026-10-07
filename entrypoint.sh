#!/bin/sh
set -eu

CONFIG_DIR="${CONFIG_DIR:-/config}"
OUTPUT_DIR="${OUTPUT_DIR:-/output}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

case "$PUID" in
  ''|*[!0-9]*) echo "ERROR: PUID must be numeric." >&2; exit 1 ;;
esac

case "$PGID" in
  ''|*[!0-9]*) echo "ERROR: PGID must be numeric." >&2; exit 1 ;;
esac

mkdir -p "$CONFIG_DIR" "$OUTPUT_DIR"

# Ensure the selected UID/GID exist so tools show a meaningful owner inside
# the container. Reuse existing IDs if the base image already has them.
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" plexepg
fi

if ! getent passwd "$PUID" >/dev/null 2>&1; then
    USER_GROUP="$(getent group "$PGID" | cut -d: -f1)"
    useradd -u "$PUID" -g "$USER_GROUP" -d "$CONFIG_DIR" -s /usr/sbin/nologin plexepg
fi

# Correct ownership of the mounted config directory and anything this
# container previously generated.
chown -R "$PUID:$PGID" "$CONFIG_DIR" "$OUTPUT_DIR"

if [ ! -f "$CONFIG_DIR/.env" ]; then
    echo "[plex-epg] No $CONFIG_DIR/.env found; Web UI setup wizard is available."
fi

echo "[plex-epg] Running as UID $PUID / GID $PGID"
exec gosu "$PUID:$PGID" python /app/server.py
