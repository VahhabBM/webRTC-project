#!/bin/sh
set -e

echo "Waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
python - <<'PY'
import os
import time

import psycopg

host = os.environ["POSTGRES_HOST"]
port = int(os.environ.get("POSTGRES_PORT", "5432"))
user = os.environ["POSTGRES_USER"]
password = os.environ["POSTGRES_PASSWORD"]
dbname = os.environ["POSTGRES_DB"]

for attempt in range(30):
    try:
        with psycopg.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=dbname,
            connect_timeout=3,
        ):
            print("PostgreSQL is available.")
            break
    except Exception as exc:
        print(f"PostgreSQL not ready ({attempt + 1}/30): {exc}")
        time.sleep(2)
else:
    raise SystemExit("PostgreSQL did not become available in time.")
PY

echo "Waiting for Redis at ${REDIS_URL}..."
python - <<'PY'
import os
import time

import redis

url = os.environ.get("REDIS_URL", "redis://redis:6379/0")

for attempt in range(30):
    try:
        client = redis.from_url(url, socket_connect_timeout=3)
        client.ping()
        print("Redis is available.")
        break
    except Exception as exc:
        print(f"Redis not ready ({attempt + 1}/30): {exc}")
        time.sleep(2)
else:
    raise SystemExit("Redis did not become available in time.")
PY

echo "Applying database migrations..."
python manage.py migrate --noinput

exec "$@"
