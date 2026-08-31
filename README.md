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

## Project structure

```text
.
├── config/
│   ├── settings/
│   │   ├── base.py          # shared settings
│   │   ├── local.py         # development
│   │   ├── staging.py       # staging
│   │   └── production.py    # production
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
├── docker/
│   └── entrypoint.sh        # wait for Postgres, migrate, then start
├── tests/
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