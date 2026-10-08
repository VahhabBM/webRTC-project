# Event-day runbook (T-55)

Operator checklist for running a live event on an already-deployed production
server. Every command, URL, port, and variable name below comes from files that
exist in this repository.

- **Deploying, updating, or rolling back is out of scope here.** That is
  [docs/deployment.md](deployment.md) (T-51) and is the single source of truth
  for it. Do not deploy during an event.
- **After a tag is cut, nothing new is deployed without project-manager
  approval.** That policy, the roles, and the rollback paragraph are
  [docs/production-version-freeze.md](production-version-freeze.md) (T-56).
- **This runbook contains no secret values.** Variable names only.
- **This runbook is not validated by deploying to a server.** Walking through
  the checks on the live server is the operator's job, before the event starts.

---

## 1. Fill this in before the event

Placeholders only — fill locally, keep out of Git, and never write secret
values here.

| Field | Value | Where it comes from |
|---|---|---|
| App server IP address | `<SERVER_IP>` | your infrastructure |
| Public app hostname | `<PUBLIC_HOST>` | must be listed in `DJANGO_ALLOWED_HOSTS` |
| TURN address | `<TURN_HOST>` | value of `COTURN_REGION1_DOMAIN` |
| Deploy directory | `<DEPLOY_DIR>` (e.g. `/srv/webrtc-project`) | `docs/deployment.md` §3.1 |
| Deployed Git tag | `<TAG>` | must equal `APP_GIT_TAG` in `.env.production` |
| Event ID | `<EVENT_ID>` | UUID from `/admin/events/event/` |
| SSH user | `<SSH_USER>` | your infrastructure |

Every `docker compose` command below assumes:

```bash
cd <DEPLOY_DIR>
docker compose --env-file .env.production -f docker-compose.prod.yml <...>
```

---

## 2. Ports the operator must know

| Port | Protocol | Purpose | Must be reachable from |
|---|---|---|---|
| 22 | TCP | SSH to the app server | operator machine only |
| 80 | TCP | HTTP on the reverse proxy; redirects to HTTPS (`SECURE_SSL_REDIRECT` is on by default) | public |
| 443 | TCP | HTTPS **and** WSS (`/ws/events/`) through the reverse proxy | public |
| 3478 | UDP + TCP | TURN/STUN on the relay host (`COTURN_PORT`, `listening-port` in `coturn/turnserver.conf`) | public |
| 49152–49999 | UDP | TURN media relay range | public |

Also in use, worth knowing:

- **5349/TCP** — TURNS (TLS), `COTURN_TLS_PORT` / `tls-listening-port`. The
  `turns:` ICE URL in `apps/events/turn.py` uses it.
- **127.0.0.1:8000** — Daphne, published on loopback only by
  `docker-compose.prod.yml`. Not public; only the reverse proxy reaches it.
- **5432 (PostgreSQL) and 6379 (Redis) must never be publicly reachable.**
  `docker-compose.prod.yml` publishes no host ports for them. See
  `docs/deployment.md` §2.

> The relay range the firewall opens must match the relay's own config. The
> Coturn files in this repo (`coturn/turnserver.conf`,
> `coturn/turnserver-region2.conf`) are **staging** and set
> `min-port=49152` / `max-port=49300`. A production relay expected to use
> 49152–49999 must have `min-port`/`max-port` set accordingly — otherwise the
> extra opened ports are never used, and relayed media is capped at the
> narrower range.

---

## 3. What to open on event day

| Open | URL | Why |
|---|---|---|
| Health | `https://<PUBLIC_HOST>/health/` | one-glance DB + Redis status |
| Admin | `https://<PUBLIC_HOST>/admin/` | log in first; everything below needs it |
| Event list | `/admin/events/event/` | find `<EVENT_ID>`, see status and participant counts |
| **Live monitoring** | `/admin/events/event/<EVENT_ID>/live-monitoring/` | main event-day screen: active round, pairs, disconnected participants, operator action log, incident notes |
| Matching report | `/admin/events/event/<EVENT_ID>/matching-report/` | only before the event starts, to generate/lock the schedule |
| SSH session | `ssh <SSH_USER>@<SERVER_IP>` (port 22) | for the `docker compose` checks in §5 and §6 |

Operator controls available on the live-monitoring page (`apps/events/admin.py`,
`apps/events/operator.py`): **pause**, **resume**, **extend by N seconds**, and
**add incident note** for a participant. Prefer these over anything at the
container level — they are logged to `OperatorActionLog` and are visible in the
action log on the same page.

Spot-checking a participant view is fine (`/room/`, `/clock-sync/`), but do not
join as a real participant while rounds are running.

---

## 4. Do not reboot, do not restart

### Rebooting the app server

**A reboot does not reliably bring the stack back. If the containers were
started without a restart policy, they will stay down after reboot and you must
start them manually.** Do not reboot during an event.

`docker-compose.prod.yml` does declare `restart: unless-stopped` for `web`,
`db`, and `redis`, so containers started with *that* file come back after a
reboot — but only if the Docker daemon itself starts at boot, and only if the
stack was actually started that way. Containers started ad hoc, with
`docker-compose.yml` (which declares **no** restart policy), or with `--rm` will
not come back at all.

Confirm this **before** the event, not during it:

```bash
# Restart policy actually applied to the running containers (expect: unless-stopped)
docker inspect --format '{{ .Name }} {{ .HostConfig.RestartPolicy.Name }}' \
  $(docker compose --env-file .env.production -f docker-compose.prod.yml ps -q)

# Docker starts on boot
systemctl is-enabled docker
```

If either answer is wrong, fix it before the event or accept that a reboot means
a manual `up -d` and several minutes of downtime.

### Containers not to restart mid-event

| Container | Restarting it causes | Do instead |
|---|---|---|
| `web` (Daphne) | Every WebSocket drops at once; all participants reconnect simultaneously; in-flight round transitions and `server.*` broadcasts are lost | Read logs first (§6). Use pause/extend from the live-monitoring page |
| `redis` | Channels layer and cache are gone: group broadcasts fail, participants see errors even though the page is up. Worse than a `web` restart | Check connectivity, not the container |
| `db` | All writes fail; `web` keeps running but errors. On restart the entrypoint re-waits for PostgreSQL and re-runs migrations | Nothing — treat as last resort |
| Coturn (relay host) | Every relayed call drops instantly and participants must renegotiate ICE | Nothing mid-round |

Never during an event: `down`, `up -d --build`, a tag change, `git checkout`, or
anything in `docs/deployment.md` §7. Never pass `-v` to `down` — it deletes the
PostgreSQL volume.

---

## 5. Status checks

```bash
# All three services up and healthy
docker compose --env-file .env.production -f docker-compose.prod.yml ps

# Application health (DB + Redis), from the server
curl -fsS http://127.0.0.1:8000/health/

# Same through the public hostname and proxy
curl -fsS https://<PUBLIC_HOST>/health/
```

### Reading `/health/`

`apps/health/views.py` checks the database connection and a Redis
set/get round-trip.

| Response | Status code | Meaning |
|---|---|---|
| `{"status": "healthy", "database": "up", "redis": "up"}` | 200 | Both dependencies reachable. Application-level problems are still possible |
| `"database": "down"` | 503 | Django cannot open a DB connection (`OperationalError`) |
| `"redis": "down"` | 503 | Cache set/get failed — WebSocket group broadcasts are also affected |
| No response / proxy error | 502/504 | Daphne or the reverse proxy is down, not the DB or Redis |
| Redirect loop | — | Proxy is not sending `X-Forwarded-Proto: https` (`docs/deployment.md` §2) |

`/health/` says nothing about TURN. Media problems never show up here.

---

## 6. Logs

Logging is configured in `config/settings/base.py`: a single **console** handler
writing **JSON** to stdout, with a `redact_sensitive` filter and a `request_id`
field. There are no log files inside the containers — read logs through Docker.

```bash
# Follow the application (Daphne, protocol handlers, health checks)
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f web

# Last 100 lines, timestamped
docker compose --env-file .env.production -f docker-compose.prod.yml logs --tail=100 -t web

# Dependencies
docker compose --env-file .env.production -f docker-compose.prod.yml logs --tail=100 redis
docker compose --env-file .env.production -f docker-compose.prod.yml logs --tail=100 db

# Relay host (name depends on your production Coturn deployment)
docker logs --tail=100 <COTURN_CONTAINER>
```

Reverse proxy and TLS logs live with the proxy, which this repository does not
ship or manage.

### Error signatures and where to look

| Signature | Look at | Likely cause |
|---|---|---|
| Browser shows 502/504, `/health/` unreachable | `logs web`, `ps` | Daphne container down or restarting |
| `/health/` returns `"redis": "down"` | `logs redis`, `logs web` | Redis container down or `REDIS_URL` wrong — channel layer broken too |
| `/health/` returns `"database": "down"` | `logs db`, `logs web` | PostgreSQL down, or `POSTGRES_*` wrong |
| `web` logs loop on `PostgreSQL not ready` / `Redis not ready` | `logs web`, `logs db`, `logs redis` | `docker/entrypoint.sh` is still waiting; `web` has not started yet |
| `ImproperlyConfigured: COTURN_SHARED_SECRET must be configured` | `logs web` | `COTURN_SHARED_SECRET` missing from `.env.production` (`apps/events/turn.py`) |
| Participants connect but see no audio/video, ICE stays `checking` | relay logs, firewall | TURN misconfigured: 3478 or the 49152–49999 UDP range blocked, or relay `min-port`/`max-port` narrower than the opened range |
| Media works on some networks only | relay logs | UDP blocked for those clients; the `turns:` 5349/TCP path must be reachable |
| ICE credentials rejected by the relay | `logs web`, relay config | `COTURN_SHARED_SECRET` does not match `static-auth-secret` on the relay |
| Page loads but WebSocket never reaches `server.hello` | proxy logs, `logs web` | Proxy not forwarding the WebSocket upgrade for `/ws/events/` |
| Admin pages load unstyled | proxy config | `collectstatic` or the proxy `/static/` route (`docs/deployment.md` §3.5) |
| Participants stuck in `disconnected` | live-monitoring page, `logs web` | Client-side network loss; check `DisconnectionLog` entries per participant |

---

## 7. If X happens, do Y

| X | Y |
|---|---|
| A single participant cannot connect | Have them reload the join link. Record an incident note on the live-monitoring page. Do not touch containers |
| Several participants lose media at once | Check the relay and the 3478 / 49152–49999 rules. Do not restart `web` |
| A round needs more time | **Extend** from the live-monitoring page. Never edit round times directly in the DB |
| Something needs to stop right now | **Pause** from the live-monitoring page, then **resume**. Do not stop containers |
| `/health/` is 503 on Redis or DB | Check that container's logs. Expect WebSocket breakage. Announce a short pause before any container action |
| Daphne is down (502 and `ps` shows `web` exited) | Read `logs web` first, capture the error, then `up -d web`. Expect every participant to reconnect |
| Reverse proxy or TLS failure | Fix at the proxy. The app stack needs no change |
| Server was rebooted | Verify with `ps`; if containers are not up, `up -d` (no `--build`), then re-run §5 |
| A wrong version appears to be running | Verify the tag per `docs/deployment.md` §6. Do **not** redeploy mid-event |
| You are unsure | Pause the round, read logs, then act. Restarting is the last option, never the first |

---

## 8. TURN configuration (T-49) — variable names only

Production ICE configuration is read from the environment by
`config/settings/production.py` and used by `apps/events/turn.py`. Names as they
appear in `.env.example`; values live only in `.env.production` on the server.

| Name | Event-day relevance |
|---|---|
| `COTURN_REGION1_DOMAIN` | The relay hostname participants reach — your `<TURN_HOST>` |
| `COTURN_PORT` | TURN/STUN port (3478) |
| `COTURN_TLS_PORT` | TURNS port (5349), the TCP/TLS fallback path |
| `COTURN_SHARED_SECRET` | Must match `static-auth-secret` on the relay. Mismatch = credentials rejected, no relayed media |
| `COTURN_CREDENTIAL_TTL_SECONDS` | Lifetime of generated credentials; too short means mid-event renegotiation failures |
| `COTURN_UDP_TRANSPORT_PARAM` | Shape of the UDP TURN URL; production default is plain `turn:host:port` |
| `COTURN_REGION2_DOMAIN` | Leave unset — no second production relay exists |
| `COTURN_REGION2_PORT`, `COTURN_REGION2_TLS_PORT`, `COTURN_REGION2_SHARED_SECRET` | Only meaningful with `COTURN_REGION2_DOMAIN` |

Do not change any of these during an event: `web` must restart to pick them up.

`docker-compose.turn.yml` and `coturn/*.conf` in this repository are the
**staging** relay configuration, with staging hostnames and a staging secret.
They must not be reused as-is in production. Full variable reference:
[docs/deployment.md](deployment.md) §4.

---

## 9. Must not be run during an event

- Anything in `docs/deployment.md` §7 (tag update, rebuild, rollback).
- `python manage.py seed_event` — synthetic data in a live event.
- `python manage.py message_layer_load_test` — load generator; it refuses
  production settings by design.
- `docker compose ... down` (and never with `-v`).
- `docker-compose.yml`, the development stack — it uses `runserver` and
  publishes 5432 and 6379.

Full list: `docs/deployment.md` §9.

---

## 10. Pre-event sign-off (do this the day before)

- [ ] §1 filled in, kept out of Git.
- [ ] `ps` shows `web`, `db`, `redis` up and healthy.
- [ ] `https://<PUBLIC_HOST>/health/` returns `healthy` with both `up`.
- [ ] Restart policy and `systemctl is-enabled docker` verified (§4).
- [ ] 5432 and 6379 not reachable from outside (`docs/deployment.md` §5.5).
- [ ] 3478 and 49152–49999 open and matching the relay's `min-port`/`max-port`.
- [ ] Admin login works; live-monitoring page loads for `<EVENT_ID>`.
- [ ] Schedule generated and locked via the matching report page.
- [ ] One end-to-end call tested with real media before participants arrive.
- [ ] Deployed tag recorded, and the previous tag noted as a rollback target
      (`docs/deployment.md` §6).
