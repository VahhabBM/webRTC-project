# WebRTC Event Platform

Django project skeleton and local Docker development environment (T-01).

## Prerequisites

- Git
- Docker Desktop (or Docker Engine + Docker Compose v2)

Python 3.12+ is only required if you run tools outside Docker.

## Clone

```bash
git clone https://github.com/VahhabBM/webRTC-project.git
cd webRTC-project
```

## Environment setup

Copy the example environment file and edit values if needed:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

`.env` is gitignored. Do not commit secrets.

Required variables are documented in `.env.example`:

- Django: `DJANGO_SETTINGS_MODULE`, `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`
- PostgreSQL: `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`
- Redis: `REDIS_URL`

For Docker Compose, keep `POSTGRES_HOST=db` and `REDIS_URL=redis://redis:6379/0`.

## Start the project

From the repository root:

```bash
docker compose up --build
```

The app is available at http://localhost:8000/

Health check: http://localhost:8000/health/

The `web` service waits for PostgreSQL, runs migrations, then starts Django. PostgreSQL and Redis start as Compose services.

## Migrations

Migrations run automatically on container start. To run them manually:

```bash
docker compose exec web python manage.py migrate
```

Create a new migration later with:

```bash
docker compose exec web python manage.py makemigrations
```

## Tests

```bash
docker compose exec web pytest
```

If the stack is not running:

```bash
docker compose run --rm web pytest
```

## Lint and format

```bash
docker compose exec web ruff check .
docker compose exec web ruff format --check .
```

Apply formatting:

```bash
docker compose exec web ruff format .
```

## Synthetic event data (T-06)

Use the `seed_event` management command to create a new synthetic Event, its rounds,
sample Tags, and Participants:

```bash
python manage.py seed_event --participants 10
python manage.py seed_event --participants 100
python manage.py seed_event --participants 900 --seed 42
```

Each invocation creates a new uniquely named Event and never deletes or overwrites
existing Events or Participants. The predefined sample Tags are reused by name, so
rerunning the command does not duplicate them. Use `--event-name`, `--description`,
`--num-rounds`, `--round-duration`, `--break-duration`, and `--start-time` to
customize Event configuration. `--seed` makes participant names and tag assignment
reproducible. When running with `DJANGO_SETTINGS_MODULE=config.settings.production`,
the explicit `--confirm-production` flag is required.

## WebSocket protocol (T-13)

The full client–server protocol contract lives in two places:

| Artifact | Purpose |
|---|---|
| `docs/protocol.md` | Human-readable specification — message schemas, lifecycle diagrams, error codes, extensibility rules |
| `apps/protocol/` | Shared Python module — constants, validators, builder helpers used by backend handlers |

### Quick reference

```python
from apps.protocol.constants import MessageType, ErrorCode, PROTOCOL_VERSION
from apps.protocol.validators import validate_message
from apps.protocol.exceptions import ProtocolError
from apps.protocol.schemas import build_server_hello, error_from_protocol_error
```

**Validating an inbound WebSocket message:**

```python
try:
    msg_type, payload = validate_message(raw_text)
except ProtocolError as exc:
    await ws.send(json.dumps(error_from_protocol_error(exc)))
```

**Building an outbound server message:**

```python
msg = build_server_hello(
    participant_id="p-abc",
    server_ts=1_700_000_001_000,
    client_ts_echo=1_700_000_000_000,
    event_id="evt-xyz",
)
await ws.send(json.dumps(msg))
```

### Extending the protocol

- **New message type (non-breaking):** add to `MessageType` → add payload validator
  in `validators.py` → add builder in `schemas.py` → document in `docs/protocol.md`.
- **Breaking change:** increment `PROTOCOL_VERSION` in `constants.py`, add the new
  version to `SUPPORTED_VERSIONS`, and document the migration in `docs/protocol.md`.
- **New error code:** add to `ErrorCode` in `constants.py` — never hard-code error
  strings in handlers.

See `docs/protocol.md` §11 and §12 for the full extensibility guide and developer
patterns.

## Project structure

```text
.
├── apps/
│   ├── health/              # health-check endpoint (T-03)
│   └── protocol/            # WebSocket protocol contract (T-13)
│       ├── constants.py     # MessageType, ErrorCode, PROTOCOL_VERSION
│       ├── exceptions.py    # ProtocolError
│       ├── validators.py    # validate_message(), validate_payload()
│       └── schemas.py       # build_server_*() helpers
├── config/
│   ├── settings/
│   │   ├── base.py          # shared settings
│   │   ├── local.py         # development
│   │   ├── staging.py       # staging
│   │   └── production.py    # production
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
├── docs/
│   └── protocol.md          # WebSocket protocol specification
├── docker/
│   └── entrypoint.sh        # wait for Postgres, migrate, then start
├── tests/
│   └── test_protocol.py     # protocol contract tests (114 cases)
├── docker-compose.yml
├── Dockerfile
├── manage.py
├── pyproject.toml           # pytest + Ruff
├── requirements.txt
├── .env.example
└── .gitignore
```

Settings modules:

- local: `config.settings.local` (default)
- staging: `config.settings.staging`
- production: `config.settings.production`

Locale: `LANGUAGE_CODE=fa-ir`, timezone `UTC`, `USE_TZ=True`.
