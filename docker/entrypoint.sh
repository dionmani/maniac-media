#!/bin/sh
# Maniac Media container entrypoint.
#
# Unraid (and linuxserver.io-style) convention: the container starts as root, then drops to the
# PUID/PGID that owns your shares (Unraid: 99:100 = nobody:users). Only /config is chowned –
# your media under /data is never touched.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-002}"
umask "$UMASK"

if [ -z "${MANIAC_MASTER_KEY:-}" ]; then
  echo "WARNING: MANIAC_MASTER_KEY is not set. A key file will be generated at ${MANIAC_KEY_FILE}."
  echo "         It lives next to your data, which is less secure. Set MANIAC_MASTER_KEY to fix this."
fi

mkdir -p "$MANIAC_DATA_DIR" "$(dirname "$MANIAC_KEY_FILE")"

if [ "$(id -u)" = "0" ]; then
  # Only fix ownership when it is wrong (cheap on every restart).
  if [ "$(stat -c %u:%g "$MANIAC_DATA_DIR")" != "$PUID:$PGID" ]; then
    echo "Setting ownership of $MANIAC_DATA_DIR to $PUID:$PGID"
    chown -R "$PUID:$PGID" "$MANIAC_DATA_DIR"
  fi
  chown "$PUID:$PGID" "$(dirname "$MANIAC_KEY_FILE")"
  chmod 700 "$(dirname "$MANIAC_KEY_FILE")"
  echo "Starting Maniac Media as $PUID:$PGID (umask $UMASK)"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --inh-caps=-all python /app/run.py "$@"
fi

# Already started as a non-root user (e.g. docker run --user 1000:1000)
exec python /app/run.py "$@"
