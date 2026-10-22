#!/bin/sh
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}
BACKEND_PORT=${BACKEND_PORT:-7331}
export BACKEND_PORT

# Add the installed PostgreSQL binaries to PATH (initdb and postgres live in
# /usr/libexec/postgresql17 on Alpine, outside the default PATH)
PG_BIN=$(ls -d /usr/libexec/postgresql*/ 2>/dev/null | head -1)
if [ -n "$PG_BIN" ]; then
    export PATH="$PG_BIN:$PATH"
fi

PGDATA=/app/postgres/data

# Timezone
if [ -n "$TZ" ]; then
    # Not fatal: /etc/localtime may be a mounted file (k8tz, -v /etc/localtime:...) that busybox ln cannot replace
    ln -snf /usr/share/zoneinfo/"$TZ" /etc/localtime 2>/dev/null || echo "Could not set /etc/localtime, keeping the existing one"
    echo "$TZ" > /etc/timezone 2>/dev/null || true
fi

# initdb and postgres refuse to run as a uid with no passwd entry, so create
# the group/user for the requested PGID/PUID (ignore errors if already exist)
addgroup -g "$PGID" scrob 2>/dev/null || true
adduser -D -H -u "$PUID" -G "$(getent group "$PGID" | cut -d: -f1)" -s /sbin/nologin scrob 2>/dev/null || true

# Allow the unprivileged user to write to Docker's stdout/stderr
chmod o+w /dev/stdout /dev/stderr

# Fix ownership of app data dir
chown -R "$PUID:$PGID" /app/backend/data

if [ -z "$DATABASE_URL" ]; then
    # ── Embedded PostgreSQL mode ───────────────────────────────────────────────
    export DATABASE_URL="postgresql+asyncpg://scrob@127.0.0.1/scrob"

    mkdir -p "$PGDATA" /var/run/postgresql
    chown -R "$PUID:$PGID" "$PGDATA" /var/run/postgresql

    # Initialise data directory on first run
    if [ ! -f "$PGDATA/PG_VERSION" ]; then
        echo "Initialising embedded PostgreSQL..."
        su-exec "$PUID:$PGID" initdb -D "$PGDATA" --auth=trust --no-locale -E UTF8 -U scrob
    fi

    # Start postgres temporarily so we can run migrations
    su-exec "$PUID:$PGID" pg_ctl -D "$PGDATA" -o "-k /var/run/postgresql -h 127.0.0.1" -l /tmp/pg-init.log start

    # Wait until postgres accepts connections
    echo "Waiting for PostgreSQL to start..."
    attempts=0
    max_attempts=150 # 150 * 0.2 = 30 seconds
    until su-exec "$PUID:$PGID" pg_isready -h 127.0.0.1 -q 2>/dev/null; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge "$max_attempts" ]; then
            echo "Error: PostgreSQL failed to start. Logs:"
            cat /tmp/pg-init.log
            exit 1
        fi
        sleep 0.2
    done

    # Create the scrob database on first run
    if ! su-exec "$PUID:$PGID" psql -h 127.0.0.1 -U scrob -d postgres -tc \
            "SELECT 1 FROM pg_database WHERE datname='scrob'" | grep -q 1; then
        su-exec "$PUID:$PGID" psql -h 127.0.0.1 -U scrob -d postgres -c "CREATE DATABASE scrob;"
    fi

    echo "Running database migrations..."
    cd /app/backend
    su-exec "$PUID:$PGID" .venv/bin/python -m alembic upgrade head

    # Stop the temporary postgres — supervisord will manage it from here
    su-exec "$PUID:$PGID" pg_ctl -D "$PGDATA" stop -m fast

    echo "Starting Scrob with embedded PostgreSQL (frontend :7330, backend 127.0.0.1:${BACKEND_PORT})..."
    exec su-exec "$PUID:$PGID" /usr/local/bin/supervisord -n -c /etc/supervisor/supervisord.omnibus.conf

else
    # ── External database mode — behaves identically to the standard image ─────
    echo "Running database migrations..."
    cd /app/backend
    su-exec "$PUID:$PGID" .venv/bin/python -m alembic upgrade head

    echo "Starting Scrob (frontend :7330, backend 127.0.0.1:${BACKEND_PORT})..."
    exec su-exec "$PUID:$PGID" /usr/local/bin/supervisord -n -c /etc/supervisor/supervisord.conf
fi
