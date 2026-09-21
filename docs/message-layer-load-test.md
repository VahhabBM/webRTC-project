# T-44 message-layer load test

Staging/local **tool** that connects N synthetic WebSocket clients to the
existing T-14 message layer, authenticates them with the real T-08 join
contract, runs **one** T-24 round transition (including a partner switch),
and writes machine-readable latency and loss statistics.

This is not a 900-client execution task. The acceptance sample is **50**
clients. The tool refuses `config.settings.production`.

The implementation lives under `tools/message_layer_load_test/` and a thin
`message_layer_load_test` management command. It is not imported by the
WebSocket consumer or orchestrator.

## What it reuses

| Layer | Existing contract |
|---|---|
| Auth | `GET /join/<token>/` then Django `sessionid` cookie (T-08 / T-14) |
| Socket | `GET /ws/events/` through `AuthMiddlewareStack` |
| Handshake | `client.hello` → `server.hello` |
| RTT | `client.clock_sync` → `server.clock_sync` (T-15 timestamps) |
| Ready | `client.ready` (idempotent T-13 signal) |
| Lifecycle | T-24 `OrchestratorRealtime`: `server.pairing` → `server.round_start` → `server.round_end` → `server.pairing` (round 2 partner switch) |

No parallel messaging protocol is introduced. Production authentication is
not weakened and no extra production-only join endpoint is added.

## Prerequisites

1. App + PostgreSQL + Redis running (Compose or staging). Redis is required
   so T-24 broadcasts on the channel layer reach the ASGI process.
2. Staging/local settings, **not** production.
3. The HTTP origin must be the same deployment the sockets will hit, so join
   cookies are valid for `/ws/events/`.
4. Staging should be HTTPS (`wss://`). Local Compose uses `http://` / `ws://`.

Optional environment (CLI flags override):

```bash
MESSAGE_LAYER_LOAD_TEST_HTTP_BASE=https://staging.example.com
MESSAGE_LAYER_LOAD_TEST_WS_URL=wss://staging.example.com/ws/events/
```

## CLI

```bash
python manage.py message_layer_load_test --help
python -m tools.message_layer_load_test --help
```

| Argument | Default | Meaning |
|---|---|---|
| `--clients` / `-n` | `50` | Even number of synthetic clients, 4–200. **50 is the acceptance sample.** |
| `--http-base` | `http://127.0.0.1:8000` | Origin used for `GET /join/<token>/` |
| `--ws-url` | derived from `--http-base` | T-14 URL, normally `ws(s)://…/ws/events/` |
| `--output` / `-o` | `message-layer-load-test-result.json` | Result JSON path (secrets stripped) |
| `--connect-concurrency` | `10` | Max simultaneous join + handshake operations |
| `--clock-sync-samples` | `5` | RTT samples per client (`client.clock_sync`) |
| `--handshake-timeout` | `10` | Seconds for join, socket open, and `server.hello` |
| `--lifecycle-timeout` | `15` | Seconds to wait for each T-24 message |
| `--settle-seconds` | `0.4` | Pause after each orchestrator broadcast |
| `--event-name` | timestamped T-44 name | Throwaway event name |
| `--cleanup` | off | Delete the throwaway event after sockets close |
| `--cookie-name` | `sessionid` | Django session cookie name |

Client count must be even (every client is paired) and at least 4 so round 2
can switch partners without violating the no-repeat-pair constraint. The tool
caps at 200 and refuses 900.

## Local Compose (50-client acceptance sample)

From the repo root, with the stack already up (`docker compose up --build`):

```bash
docker compose exec web python manage.py message_layer_load_test \
  --clients 50 \
  --http-base http://127.0.0.1:8000 \
  --ws-url ws://127.0.0.1:8000/ws/events/ \
  --output /app/message-layer-load-test-result.json \
  --connect-concurrency 10
```

Copy the JSON out if you ran inside the container:

```bash
docker compose cp web:/app/message-layer-load-test-result.json ./message-layer-load-test-result.json
```

On the host (venv, same `.env`, app listening on :8000):

```bash
python manage.py message_layer_load_test --clients 50
```

## Staging

Use staging Django settings **in the process that provisions participants**
(so join tokens are issued against the staging database) and point HTTP/WS
at the public staging origin:

```bash
DJANGO_SETTINGS_MODULE=config.settings.staging \
python manage.py message_layer_load_test \
  --clients 50 \
  --http-base https://staging.example.com \
  --ws-url wss://staging.example.com/ws/events/ \
  --output ./message-layer-load-test-result.json \
  --connect-concurrency 10 \
  --cleanup
```

If the command runs inside the staging web container, `--http-base` may be
the in-cluster origin (for example `http://127.0.0.1:8000`) as long as that
host sets the session cookie the WebSocket handshake will send.

`--cleanup` deletes the throwaway T-44 event after the run. Omit it if you
want to inspect pairs in admin first.

## Result JSON

The file is UTF-8 JSON. Tokens, cookies, join URLs, and keys whose names
contain `token` / `cookie` / `session` / `password` / `secret` /
`authorization` / `credential` are redacted.

Inspect at least:

- `config.clients` = 50 (or the count you passed)
- `config.http_base`, `config.ws_url`, `config.event_id`, timestamps
- `clients.attempted` / `connected` / `handshake_ok` / `failed`
- `latency_ms` (`min`, `max`, `mean`, `p50`, `p95`, `p99`, `sample_count`)
- `messages.sent`, `messages.expected`, `messages.received`
- `messages.lost` / `messages.loss_count` / `messages.loss_rate`
- `messages.by_type` for `server.hello`, `server.clock_sync`,
  `server.pairing`, `server.round_start`, `server.round_end`
- `lifecycle.sequence` showing pairing → ready → round_start → round_end →
  pairing (round 2)

Per-client failures are listed under `clients.failures` and do not abort the
run. Sockets are closed in a `finally` block.

## Focused automated tests

```bash
pytest tests/test_message_layer_load_test.py
ruff check tools tests/test_message_layer_load_test.py apps/events/management/commands/message_layer_load_test.py
ruff format --check tools tests/test_message_layer_load_test.py apps/events/management/commands/message_layer_load_test.py
```

Do **not** run the 50-client command in CI. It needs a live ASGI server,
Redis, and staging/local data.

## T-30 regression (manual, before merge)

Use two real browsers, not the load-test sockets:

1. `python manage.py create_test_pair`
2. Open the two printed `/join/<token>/` URLs, then open `/room/` in each window.
3. `python manage.py orchestrator_broadcast --event <event-uuid> pairing --round 1`
   - Evidence: partner name/tags and room appear on both clients (call-room pairing).
4. `python manage.py orchestrator_broadcast --event <event-uuid> round_start --round 1`
   - Evidence: both timers leave waiting, count down together from `round_end_ts`
     (synchronized timer). Camera/mic are not re-prompted.
5. `python manage.py orchestrator_broadcast --event <event-uuid> warning --round 1`
   then `round_end`
   - Evidence: warning state, then round-ending UI (round rotation).
6. Partner switch: use an event with two rounds (or the T-44 throwaway event
   after a 4+ client provision without `--cleanup`), broadcast `pairing --round 2`.
   - Evidence: new partner/room, local media reused, no extra getUserMedia prompt.
7. Confirm the original pair still cannot see another room, and a third browser
   without a join session is rejected on `/ws/events/` with `ERR_NOT_AUTHENTICATED`.
