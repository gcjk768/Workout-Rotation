#!/bin/sh
# Starts as root only to give the coach user its folders, then runs the bot as that user,
# so files in ./data belong to your NAS account (PUID/PGID in bot.env) instead of root.
set -e
PUID="${PUID:-$(id -u coach)}"
PGID="${PGID:-$(id -g coach)}"
if [ "$(id -u)" = "0" ]; then
    mkdir -p /data /work
    chown -R "$PUID:$PGID" /data /work
    # The Obsidian vault (VAULT_DIR): only the folder itself, the owner's own notes keep their owner.
    if [ -n "$VAULT_DIR" ]; then
        mkdir -p "$VAULT_DIR" && chown "$PUID:$PGID" "$VAULT_DIR" || echo "Could not prepare $VAULT_DIR" >&2
    fi
    if [ "$(stat -c %u /home/coach)" != "$PUID" ]; then
        chown -R "$PUID:$PGID" /home/coach
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups env HOME=/home/coach USER=coach "$@"
fi
exec "$@"
