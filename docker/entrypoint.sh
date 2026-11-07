#!/bin/sh
set -eu

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    attempt=1
    while ! python -m alembic upgrade head; do
        if [ "$attempt" -ge 30 ]; then
            echo "alembic upgrade failed after $attempt attempts" >&2
            exit 1
        fi
        echo "database is not ready for migrations (attempt $attempt/30), retrying..." >&2
        attempt=$((attempt + 1))
        sleep 1
    done
fi

exec "$@"
