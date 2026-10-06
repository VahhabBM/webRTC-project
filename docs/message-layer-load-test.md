# T-44 message-layer load test

Staging/local **tool** that connects N synthetic WebSocket clients to the
existing T-14 message layer, authenticates them with the real T-08 join
contract, runs **one** T-24 round transition (including a partner switch),
and writes machine-readable latency and loss statistics.

The implementation lives under `tools/message_layer_load_test/` and a thin
`message_layer_load_test` management command. It is not imported by the
WebSocket consumer or orchestrator. The tool refuses
`config.settings.production`.

## Who runs what (T-52)

| Run | Clients | Owner | Where |
|---|---|---|---|
| Automated regression | 4 (fakes, no sockets) | Developer / CI | `pytest tests/test_message_layer_load_test.py` |
| Acceptance sample | 50 | Developer or operator | Local Compose or staging, live ASGI + Redis |
| **Nominal target** | **900** | **Server operator** | Operator-controlled staging/production-like deployment |

The **nominal target is 900 synthetic clients**. That run is the **server
operator's responsibility, not the developer's**: it needs operator-sized
hardware, operator-controlled Redis/Postgres, and an operator-approved
deployment window. Developers and CI must not attempt it.

Note the real limit shipped in this repo: `validate_client_count()` in
`tools/message_layer_load_test/provision.py` accepts an **even count between
`MIN_CLIENT_COUNT` (4) and `MAX_CLIENT_COUNT` (200)** per invocation, and
raises an explicit error for 900. Those bounds are defined in
`tools/message_layer_load_test/__init__.py`. Reaching the 900-client nominal
target therefore requires an explicit decision by the operator to raise
`MAX_CLIENT_COUNT`; the tool as shipped will not do it on its own, and this
document does not claim otherwise.

## The exact command

One command produces one result file:

```bash
python manage.py message_layer_load_test --clients 50 --output ./message-layer-load-test-result.json
```

Everything else on this page is a variation of that command (Compose,
staging origin, cleanup). The result file path comes from `--output`; its
contents are described under [Result JSON](#result-json).

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

### Environment variables

Variable **names** only — never commit values, and never paste a real
token, cookie, password, or secret key into this file, into the result
JSON, or into a shell history that is shared.

| Name | Required | Read by | Purpose |
|---|---|---|---|
| `DJANGO_SETTINGS_MODULE` | Required | Django | Must select local or staging settings. The tool aborts on `config.settings.production`. |
| `MESSAGE_LAYER_LOAD_TEST_HTTP_BASE` | Optional | `runner.default_http_base()` | Default for `--http-base`. Falls back to `http://127.0.0.1:8000`. |
| `MESSAGE_LAYER_LOAD_TEST_WS_URL` | Optional | `runner.default_ws_url()` | Default for `--ws-url`. Falls back to a value derived from `--http-base`. |

The tool itself defines no other environment variables. It does inherit the
normal Django/database/Redis configuration of whichever settings module and
`.env` the process already uses (`DJANGO_SECRET_KEY`, `POSTGRES_*`,
`REDIS_URL`, …) — see `.env.example` for those names. CLI flags override
`MESSAGE_LAYER_LOAD_TEST_*`.

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

The file written at `--output` is UTF-8 JSON, indented and key-sorted.
Tokens, cookies, join URLs, and keys whose names contain `token` / `cookie`
/ `session` / `password` / `secret` / `authorization` / `credential` are
redacted before the file is written, so the result file is safe to attach to
a ticket.

Top-level shape:

```json
{
  "task": "T-44",
  "tool": "message_layer_load_test",
  "started_at": "...Z",
  "finished_at": "...Z",
  "duration_ms": 0,
  "config": {},
  "clients": {},
  "latency_ms": {},
  "messages": {},
  "lifecycle": {},
  "client_snapshots": []
}
```

### Latency

`latency_ms` holds round-trip times in **milliseconds**, measured from
`client.clock_sync` send to the matching `server.clock_sync` reply
(`--clock-sync-samples` samples per client).

| Field | Type | Meaning |
|---|---|---|
| `latency_ms.sample_count` | int | Number of RTT samples across all clients |
| `latency_ms.min` | float \| null | Fastest RTT |
| `latency_ms.max` | float \| null | Slowest RTT |
| `latency_ms.mean` | float \| null | Arithmetic mean RTT |
| `latency_ms.p50` | float \| null | Median RTT |
| `latency_ms.p95` | float \| null | 95th percentile RTT |
| `latency_ms.p99` | float \| null | 99th percentile RTT |

Values are `null` when no sample was collected.

### Lost messages

`messages` holds the loss accounting. A message is "lost" when it was
expected (the tool knows how many `server.*` frames each connected client
should receive) but never arrived before the timeout.

| Field | Type | Meaning |
|---|---|---|
| `messages.sent` | int | `client.*` frames the synthetic clients sent |
| `messages.expected` | int | `server.*` frames that should have arrived |
| `messages.received` | int | `server.*` frames that did arrive |
| `messages.matched_expected` | int | Received frames counted against expectations |
| `messages.lost` | int | **Lost-message count** = `expected - matched_expected` |
| `messages.loss_count` | int | Same value as `messages.lost` (alias) |
| `messages.loss_rate` | float | `lost / expected`, `0.0` when nothing was expected |
| `messages.by_type` | object | Per message type, the same `expected`/`received`/`lost`/`loss_rate` breakdown |
| `messages.sent_by_type`, `messages.received_by_type` | object | Raw per-type counters |

`messages.by_type` is keyed by `server.hello`, `server.clock_sync`,
`server.pairing`, `server.round_start`, and `server.round_end`.

### Everything else

- `config.clients` is the count you passed; `config.http_base`,
  `config.ws_url`, `config.event_id`, `config.event_name`,
  `config.round_numbers`, `config.pair_counts`, and the timeout settings
  record how the run was configured.
- `clients.attempted` / `authenticated` / `connected` / `handshake_ok` /
  `failed`, plus `clients.failures[]` with `index`, `participant_id`,
  `stage`, and a redacted `error`.
- `lifecycle.sequence` shows pairing → ready → round_start → round_end →
  pairing (round 2); `lifecycle.broadcasts[]` records each orchestrator
  broadcast.
- `client_snapshots[]` repeats `sent` / `expected` / `received` / `lost` /
  `latency_sample_count` per client.

Per-client failures do not abort the run. Sockets are closed in a `finally`
block.

## Focused automated tests

```bash
pytest tests/test_message_layer_load_test.py
ruff check tools tests/test_message_layer_load_test.py apps/events/management/commands/message_layer_load_test.py
ruff format --check tools tests/test_message_layer_load_test.py apps/events/management/commands/message_layer_load_test.py
```

`test_small_client_run_writes_latency_and_loss_result_file` drives the
`message_layer_load_test` management command end to end with **4** clients
over in-memory fakes and asserts the `--output` file contains the
`latency_ms` and `messages.lost` / `messages.loss_count` fields documented
above. It opens no sockets and needs no server.

Do **not** run the 50-client or 900-client command in CI. Both need a live
ASGI server, Redis, and staging/local data; 900 is operator-owned (see
[Who runs what](#who-runs-what-t-52)).

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
