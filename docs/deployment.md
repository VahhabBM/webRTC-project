# Production deployment from a Git tag (T-51)

This is the single, repeatable procedure for deploying the WebRTC Event Platform
to a production server from a specific Git tag. Every step below uses files that
exist in this repository: `Dockerfile`, `docker/entrypoint.sh`,
`docker-compose.prod.yml`, and `config/settings/production.py`.

Nothing here requires oral instructions, and nothing here contains a secret
value. Only variable **names** are documented; the values live on the server.

Running an event on an already-deployed server is a separate document:
[docs/event-day-runbook.md](event-day-runbook.md) (T-55).

> Development uses `docker-compose.yml` (Django `runserver`, published database
> and Redis ports, source bind mount). **That file must never be used in
> production.** Production uses `docker-compose.prod.yml` exclusively.

---

## 1. Prerequisites on the server

- Git
- Docker Engine + Docker Compose v2
- A reverse proxy already terminating TLS for the public hostname (this repo
  does not ship or manage the proxy)
- Outbound access to the Coturn relay configured in T-49

All commands below are run from the deployment directory on the server, which is
a normal clone of this repository.

---

## 2. Hard rules

1. **PostgreSQL (5432) and Redis (6379) must never be publicly exposed.**
   `docker-compose.prod.yml` declares no `ports:` for `db` and `redis`, so they
   are reachable only on the internal Compose network. Do not add host port
   mappings for them, and keep them blocked at the firewall.
2. **Web traffic goes through Daphne behind the production reverse proxy.**
   The `web` service runs
   `daphne -b 0.0.0.0 -p 8000 config.asgi:application` and publishes only
   `127.0.0.1:8000`. `manage.py runserver` is never used in production.
3. **Secrets never enter Git.** `.env.production` is gitignored. Never commit it,
   never paste real values into this document, tickets, or chat.
4. HTTP and WebSocket traffic (`/ws/events/`, see `apps/events/routing.py`)
   must both be proxied to
   `127.0.0.1:8000`, with WebSocket upgrade headers and
   `X-Forwarded-Proto: https` set. `config/settings/production.py` sets
   `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` and enables
   `SECURE_SSL_REDIRECT` by default, so a proxy that omits this header causes a
   redirect loop.

---

## 3. First deployment

### 3.1 Clone and check out the tag

```bash
git clone https://github.com/VahhabBM/webRTC-project.git /srv/webrtc-project
cd /srv/webrtc-project
git fetch --tags --force
git checkout --detach tags/<TAG>
git describe --tags --exact-match HEAD    # must print <TAG>
```

A detached checkout of the tag is deliberate: the deployed commit can never be
moved by a later branch update.

### 3.2 Create the production environment file

Create `/srv/webrtc-project/.env.production`, owned by the deploy user with mode
`600`. It is gitignored. Fill in the variables listed in
[section 4](#4-required-production-environment-variables) — names only are given
there; the values are yours.

```bash
install -m 600 /dev/null .env.production
# edit .env.production with your editor
```

`APP_GIT_TAG` in this file must be set to the same `<TAG>` you checked out. It is
consumed by Docker Compose (not by Django) to tag the built image, which is how
the running tag is verified later.

### 3.3 Build and start the stack

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
```

On start, `docker/entrypoint.sh` waits for PostgreSQL, waits for Redis, and runs
`python manage.py migrate --noinput` before Daphne is executed. Database and
Redis readiness are also enforced by Compose `healthcheck` + `depends_on`.

### 3.4 Migrations

Migrations are applied automatically by the entrypoint on every container start,
so no manual step is normally required. To apply or inspect them explicitly:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml exec web \
  python manage.py migrate --noinput
docker compose --env-file .env.production -f docker-compose.prod.yml exec web \
  python manage.py showmigrations
```

### 3.5 Static files

`DEBUG=False` in production, so Django does not serve static files. Collect them
into the `static_files` volume (mounted at `/app/staticfiles`, matching
`STATIC_ROOT`):

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml exec web \
  python manage.py collectstatic --noinput
```

Serve `/static/` from that volume in the reverse proxy. Its host path is:

```bash
docker volume inspect $(basename $PWD)_static_files --format '{{ .Mountpoint }}'
```

Without this step the Django admin and the operator pages load without CSS/JS.

### 3.6 Administrative user (first deployment only)

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml exec web \
  python manage.py createsuperuser
```

---

## 4. Required production environment variables

Names only. Never store real values in this repository.

### Django core

| Name | Notes |
|---|---|
| `DJANGO_SETTINGS_MODULE` | Must be `config.settings.production`. `config/asgi.py` otherwise defaults to the local settings module. |
| `DJANGO_SECRET_KEY` | Required; startup fails if unset. |
| `DJANGO_ALLOWED_HOSTS` | Required; comma-separated public hostnames. Production reads it with no fallback. |
| `DJANGO_SECURE_SSL_REDIRECT` | Optional, defaults to enabled. Leave enabled unless the proxy already handles the redirect. |

`DJANGO_DEBUG` is ignored in production: `config/settings/production.py` forces
`DEBUG = False`.

### PostgreSQL

| Name | Notes |
|---|---|
| `POSTGRES_DB` | Also consumed by the `db` container. |
| `POSTGRES_USER` | Also consumed by the `db` container. |
| `POSTGRES_PASSWORD` | Also consumed by the `db` container. |
| `POSTGRES_HOST` | `db` when using `docker-compose.prod.yml`. |
| `POSTGRES_PORT` | `5432` on the internal network; never published to the host. |

### Redis

| Name | Notes |
|---|---|
| `REDIS_URL` | `redis://redis:6379/0` on the internal network. Backs both the cache and the Channels layer. |

### T-49 production TURN relay

Production ICE configuration is read from the environment by
`config/settings/production.py` and used by `apps/events/turn.py`.

| Name | Notes |
|---|---|
| `COTURN_REGION1_DOMAIN` | Primary relay hostname. |
| `COTURN_PORT` | Plain TURN/STUN port. |
| `COTURN_TLS_PORT` | TURNS (TLS) port. |
| `COTURN_SHARED_SECRET` | Required. Must match `static-auth-secret` on the Coturn server. Credential generation raises `ImproperlyConfigured` when empty. |
| `COTURN_CREDENTIAL_TTL_SECONDS` | Lifetime of generated temporary credentials. |
| `COTURN_UDP_TRANSPORT_PARAM` | Controls whether the UDP URL carries `?transport=udp`; production defaults to the plain `turn:host:port` form. |
| `COTURN_REGION2_DOMAIN` | Leave unset. Region 2 stays disabled until a second production relay exists. |
| `COTURN_REGION2_PORT` | Only with `COTURN_REGION2_DOMAIN`. |
| `COTURN_REGION2_TLS_PORT` | Only with `COTURN_REGION2_DOMAIN`. |
| `COTURN_REGION2_SHARED_SECRET` | Only with `COTURN_REGION2_DOMAIN`; falls back to the region 1 secret. |

The Coturn server itself is deployed separately. `docker-compose.turn.yml` and
`coturn/*.conf` in this repository are the staging relay configuration and must
not be reused as-is in production: they carry staging hostnames and a staging
secret.

### Event / protocol runtime (optional, defaults in `config/settings/base.py`)

| Name | Notes |
|---|---|
| `PROTOCOL_RATE_LIMIT_MESSAGES_PER_MINUTE` | Default 60. |
| `WEBSOCKET_HEARTBEAT_INTERVAL_SECONDS` | Default 30. |
| `WEBSOCKET_HEARTBEAT_TIMEOUT_SECONDS` | Default 90. |
| `WEBSOCKET_MAX_MESSAGE_BYTES` | Default 65536. |
| `PARTNER_ABSENCE_GRACE_SECONDS` | Default 25 (T-34). |
| `ORCHESTRATOR_FINAL_SECONDS` | Default 30 (T-24 round warning). |
| `CLOCK_SYNC_SAMPLE_COUNT` | Default 5. |
| `CLOCK_SYNC_INTERVAL_SECONDS` | Default 30. |
| `CLOCK_SYNC_MAX_RTT_MS` | Default 2000. |
| `MEDIA_FALLBACK_ROOM_ID` | T-38; leave empty in normal production use. |
| `MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS` | T-39; default 6000. |
| `PARTICIPANT_JOIN_BASE_URL` | Public base URL used to build join links. |
| `FRONTEND_URL` | Public base URL used in registration/magic-link emails. |
| `DJANGO_EMAIL_BACKEND` | Defaults to the console backend; set a real backend to send registration email. |
| `DEFAULT_FROM_EMAIL` | Sender address for registration email. |

SMTP host/port/credentials are not currently read from the environment by
`config/settings/base.py`. Sending real email therefore needs a backend that
does not require them, or a follow-up settings change — do not assume
`EMAIL_HOST`-style variables work today.

### Deployment-only

| Name | Notes |
|---|---|
| `APP_GIT_TAG` | Read by Docker Compose, not by Django. Tags the built image as `webrtc-project:<tag>` so the deployed tag is verifiable. Compose refuses to start if it is unset. |

---

## 5. Startup and health checks

1. All three services are up and healthy:

   ```bash
   docker compose --env-file .env.production -f docker-compose.prod.yml ps
   ```

2. The entrypoint reached Daphne (logs show PostgreSQL available, Redis
   available, migrations applied, then Daphne listening):

   ```bash
   docker compose --env-file .env.production -f docker-compose.prod.yml logs --tail=50 web
   ```

3. Application health endpoint — checks the database and Redis and returns 200
   only when both are up (`apps/health/views.py`):

   ```bash
   curl -fsS http://127.0.0.1:8000/health/
   # {"status": "healthy", "database": "up", "redis": "up"}
   ```

4. Same endpoint through the public hostname over TLS:

   ```bash
   curl -fsS https://<PUBLIC_HOST>/health/
   ```

5. No database or Redis port is reachable from outside. From a machine that is
   not the server, both of these must fail to connect:

   ```bash
   nc -vz <PUBLIC_HOST> 5432
   nc -vz <PUBLIC_HOST> 6379
   ```

---

## 6. Verify the deployment

- **Which tag is checked out:**

  ```bash
  git -C /srv/webrtc-project describe --tags --exact-match HEAD
  git -C /srv/webrtc-project rev-parse HEAD
  ```

- **Which tag is actually running** (image tag of the live container):

  ```bash
  docker compose --env-file .env.production -f docker-compose.prod.yml images web
  docker inspect --format '{{ .Config.Image }}' \
    $(docker compose --env-file .env.production -f docker-compose.prod.yml ps -q web)
  ```

  The image tag must equal the Git tag from the previous command. If they
  differ, the stack was not rebuilt after the checkout — rerun section 7.

- **Admin and participant surfaces respond** through the public hostname:
  `/admin/` loads with styling (confirms `collectstatic` plus the proxy static
  route) and `/health/` returns `healthy`.

- **WebSocket upgrade works** through the proxy: open a call room page and
  confirm the client reaches `server.hello` rather than falling back to repeated
  reconnects. Any WebSocket failure here is a proxy upgrade-header problem, not
  an application problem.

---

## 7. Update from one tag to another

```bash
cd /srv/webrtc-project

# 1. Record the currently deployed tag so a rollback target is known.
git describe --tags --exact-match HEAD

# 2. Back up the database before migrations run.
docker compose --env-file .env.production -f docker-compose.prod.yml exec -T db \
  sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > /var/backups/webrtc-$(date +%F-%H%M).sql

# 3. Fetch and check out the new tag.
git fetch --tags --force
git checkout --detach tags/<NEW_TAG>
git describe --tags --exact-match HEAD

# 4. Update APP_GIT_TAG in .env.production to <NEW_TAG>.

# 5. Rebuild and restart. Migrations run from the entrypoint on startup.
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build

# 6. Refresh static files.
docker compose --env-file .env.production -f docker-compose.prod.yml exec web \
  python manage.py collectstatic --noinput

# 7. Re-run every check in sections 5 and 6.
```

Steps 3–6 are the only difference between a first deployment and an update;
`.env.production` and the `postgres_data` / `redis_data` volumes are preserved.

**Rollback:** repeat the same procedure with the previous tag. Note that a
rollback does not revert applied migrations — restore the dump from step 2 if
the newer tag introduced schema changes that the older tag cannot read.

---

## 8. Stopping and logs

```bash
# Stop, keeping data volumes
docker compose --env-file .env.production -f docker-compose.prod.yml down

# Follow logs
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f web
```

Never pass `-v` to `down` in production: it deletes the PostgreSQL volume.

---

## 9. What must not be run in production

- `python manage.py runserver` — development only.
- `docker compose up` with the default `docker-compose.yml` — it uses
  `runserver`, publishes 5432 and 6379, and bind-mounts the working tree.
- `python manage.py seed_event` — synthetic data. It requires an explicit
  `--confirm-production` flag under production settings for this reason.
- `python manage.py message_layer_load_test` — the tool refuses production
  settings by design (see `docs/message-layer-load-test.md`).
