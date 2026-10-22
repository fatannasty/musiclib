#!/bin/sh
set -e

CONFIG=/data/config.toml

if [ ! -f "$CONFIG" ]; then
    echo "no config found, creating one at $CONFIG (library=/music)"
    musiclib init --library /music --config "$CONFIG"
fi

if [ "$(id -u)" = "0" ] && [ "${PUID:-0}" != "0" ]; then
    chown -R "${PUID}:${PGID:-$PUID}" /data
    exec setpriv --reuid="${PUID}" --regid="${PGID:-$PUID}" --clear-groups \
        musiclib serve --host 0.0.0.0 --port "${PORT:-8000}" --config "$CONFIG"
fi

exec musiclib serve --host 0.0.0.0 --port "${PORT:-8000}" --config "$CONFIG"
