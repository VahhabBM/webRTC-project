# WebRTC Event Platform — WebSocket Protocol Specification

**Version:** 1  
**Status:** Draft — awaiting review by Ali  
**Last updated:** 2026-09-01

---

## Table of contents

1. [Overview](#1-overview)
2. [Transport](#2-transport)
3. [Message envelope](#3-message-envelope)
4. [Protocol version negotiation](#4-protocol-version-negotiation)
5. [Clock synchronisation](#5-clock-synchronisation)
6. [Complete session lifecycle](#6-complete-session-lifecycle)
7. [Message reference](#7-message-reference)
   - [Handshake](#71-handshake)
   - [Clock sync & keepalive](#72-clock-sync--keepalive)
   - [Round lifecycle](#73-round-lifecycle)
   - [Partner state](#74-partner-state)
   - [WebRTC signalling](#75-webrtc-signalling)
   - [TURN credentials](#76-turn-credentials)
   - [Event end](#77-event-end)
   - [Errors](#78-errors)
8. [Error codes](#8-error-codes)
9. [Field type reference](#9-field-type-reference)
10. [Validation rules](#10-validation-rules)
11. [Extensibility](#11-extensibility)
12. [Developer guide](#12-developer-guide)
13. [Open questions for Ali](#13-open-questions-for-ali)

---

## 1. Overview

This document defines the complete client–server protocol for the WebRTC Event
Platform's WebSocket layer. The protocol coordinates:

- Connection establishment and identity verification
- Clock synchronisation across ~900 concurrent participants
- 6-round pairing assignment (7 minutes per round, 2 participants per room)
- WebRTC signalling message forwarding (offer / answer / ICE candidates)
- TURN credential distribution
- Clean event shutdown

The protocol is **text-based** (UTF-8 JSON). Every message is a single JSON
object sent as one WebSocket text frame.

> **Scope note:** This document is a *contract* specification. The full
> WebSocket consumer, ASGI routing, and WebRTC peer connection logic are
> implemented in later tasks. The shared Python module
> `apps/protocol/` provides constants, validators, and builder helpers that
> both future backend and (via a shared schema) future frontend code MUST use.

---

## 2. Transport

| Property | Value |
|---|---|
| Protocol | WebSocket (RFC 6455) |
| Encoding | UTF-8 text frames only |
| Max frame size | 64 KiB (server MUST close with 1009 if exceeded) |
| Keepalive | Application-level `client.ping` / `server.pong` (see §7.2). WebSocket ping frames MAY also be used at the transport level. |
| Authentication | Participant token supplied in `client.hello` (see §7.1) |
| TLS | Required in staging and production |

---

## 3. Message envelope

Every message — in both directions — MUST conform to this envelope:

```json
{
  "type":    "<string>",
  "version": <integer>,
  "payload": { ... }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | string | yes | Unique message type identifier (see §7). |
| `version` | integer | yes | Protocol version used by the sender. Always `1` in v1. |
| `payload` | object | yes | Message-specific data. May be an empty object `{}` but must be present. |

The envelope MUST contain exactly these three keys at the top level. Additional
top-level keys are ignored for forward compatibility but SHOULD NOT be sent.

**Example — minimal valid envelope:**

```json
{
  "type": "client.ping",
  "version": 1,
  "payload": { "client_ts": 1700000000000 }
}
```

---

## 4. Protocol version negotiation

1. Client opens the WebSocket connection.
2. Client immediately sends `client.hello` with its `version` field set to the
   protocol version it supports.
3. Server checks `version` against `SUPPORTED_VERSIONS`.
   - If supported → server replies with `server.hello`, listing all versions it
     supports in `supported_versions`.
   - If **not** supported → server replies with `server.error`
     (code `ERR_VERSION_MISMATCH`) and closes the connection with WebSocket
     close code 4000.
4. All subsequent messages from the client MUST use the same version sent in
   `client.hello`. The server MAY reject messages with a different version with
   `ERR_VERSION_MISMATCH`.

**Current supported versions:** `[1]`

Incrementing `PROTOCOL_VERSION` in `apps/protocol/constants.py` constitutes a
breaking change. Non-breaking additions (new optional fields, new message
types) do NOT require a version bump.

---

## 5. Clock synchronisation

The server is the authoritative clock. Clients estimate the server clock offset
using the following procedure:

1. Client records `t0 = Date.now()` (ms).
2. Client sends `client.clock_sync` with `client_ts = t0`.
3. Server records `server_ts` and replies with `server.clock_sync`,
   echoing `client_ts`.
4. Client records `t1 = Date.now()` on receipt.
5. Client estimates:
   - RTT = `t1 − t0`
   - Server offset = `server_ts − (t0 + RTT/2)`

Clients SHOULD perform at least one sync immediately after `server.hello` and
MAY repeat it every 30 s. The `client.hello` message also carries `client_ts`
so the server can compute an initial offset without a separate round-trip.

All timestamps in this protocol are **Unix time in milliseconds** (integer).

---

## 6. Complete session lifecycle

```
Client                                          Server
  |                                               |
  |-- [WebSocket connect] ----------------------->|
  |-- client.hello -------------------------------->|
  |<- server.hello --------------------------------|  (or server.error ERR_VERSION_MISMATCH → close)
  |                                               |
  |-- client.clock_sync -------------------------->|  (optional, can repeat)
  |<- server.clock_sync ---------------------------|
  |                                               |
  |   [Server creates pairings for round 1]       |
  |<- server.pairing ------------------------------|
  |<- server.turn_credentials --------------------|  (may follow immediately)
  |                                               |
  |-- client.ready -------------------------------->|
  |                                               |
  |<- server.round_start --------------------------|
  |                                               |
  |   [WebRTC negotiation — both peers in room]   |
  |-- client.webrtc.offer ------------------------->|
  |   (server forwards to partner)                |
  |<- server.webrtc.offer -------------------------|  (received by partner)
  |<- server.webrtc.answer ------------------------|
  |-- client.webrtc.answer ------------------------>|
  |<-> server.webrtc.ice_candidate / client.webrtc.ice_candidate (trickle ICE)
  |                                               |
  |   [7-minute round elapses]                    |
  |<- server.round_end ----------------------------|
  |                                               |
  |   [Repeat server.pairing → round_start → round_end for rounds 2–6]
  |                                               |
  |<- server.event_end ----------------------------|
  |   [Client closes WebSocket]                   |
```

If the partner disconnects mid-round:

```
  |<- server.partner_state { state: "disconnected" }
  |<- server.partner_state { state: "reconnecting" }   (if reconnect attempt)
  |<- server.partner_state { state: "connected" }       (if reconnected)
```

If the client sends an invalid message at any point:

```
  |-- <invalid message> -------------------------->|
  |<- server.error --------------------------------|  (connection remains open)
```

---

## 7. Message reference

### Field notation

| Notation | Meaning |
|---|---|
| **bold** | Required field |
| *italic* | Optional field |
| `ts` suffix | Unix timestamp in **milliseconds** (integer) |

---

### 7.1 Handshake

#### `client.hello`

Direction: **Client → Server**  
Sent: Immediately after the WebSocket connection opens. MUST be the first message.

| Field | Type | Required | Description |
|---|---|---|---|
| `participant_token` | string | yes | Opaque authentication token (JWT or signed session token). Non-empty. |
| `client_ts` | integer | yes | Client Unix timestamp in ms at the moment of sending. Used for initial clock offset estimation. Must be > 0. |

```json
{
  "type": "client.hello",
  "version": 1,
  "payload": {
    "participant_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "client_ts": 1700000000000
  }
}
```

**Validation errors:**
- `ERR_INVALID_MESSAGE` — `participant_token` is blank or missing; `client_ts` is missing, non-integer, or ≤ 0.
- `ERR_NOT_AUTHENTICATED` — token is invalid / expired (server-side check, not schema).
- `ERR_ALREADY_CONNECTED` — same participant already connected.

---

#### `server.hello`

Direction: **Server → Client**  
Sent: In response to a valid `client.hello`.

| Field | Type | Required | Description |
|---|---|---|---|
| `participant_id` | string | yes | Server-assigned, stable identifier for this participant. Non-empty. |
| `server_ts` | integer | yes | Server Unix timestamp in ms at the moment of sending. Must be > 0. |
| `client_ts_echo` | integer | yes | Echo of `client_ts` from `client.hello`, for clock offset computation. Must be > 0. |
| `event_id` | string | yes | Unique identifier for the current event. Non-empty. |
| `supported_versions` | integer[] | yes | Non-empty list of protocol versions the server supports. |

```json
{
  "type": "server.hello",
  "version": 1,
  "payload": {
    "participant_id": "p-abc123",
    "server_ts": 1700000001000,
    "client_ts_echo": 1700000000000,
    "event_id": "evt-xyz987",
    "supported_versions": [1]
  }
}
```

---

### 7.2 Clock sync & keepalive

#### `client.clock_sync`

Direction: **Client → Server**

| Field | Type | Required | Description |
|---|---|---|---|
| `client_ts` | integer | yes | Client Unix timestamp in ms. Must be > 0. |

```json
{
  "type": "client.clock_sync",
  "version": 1,
  "payload": { "client_ts": 1700000030000 }
}
```

---

#### `server.clock_sync`

Direction: **Server → Client**

| Field | Type | Required | Description |
|---|---|---|---|
| `client_ts_echo` | integer | yes | Echo of `client_ts` from `client.clock_sync`. Must be > 0. |
| `server_ts` | integer | yes | Server Unix timestamp in ms when the message was sent. Must be > 0. |

```json
{
  "type": "server.clock_sync",
  "version": 1,
  "payload": {
    "client_ts_echo": 1700000030000,
    "server_ts": 1700000030042
  }
}
```

---

#### `client.ping`

Direction: **Client → Server**  
Sent: Periodically (recommended every 30 s) to prevent idle disconnects.

| Field | Type | Required | Description |
|---|---|---|---|
| `client_ts` | integer | yes | Client Unix timestamp in ms. Must be > 0. |

```json
{
  "type": "client.ping",
  "version": 1,
  "payload": { "client_ts": 1700000060000 }
}
```

---

#### `server.pong`

Direction: **Server → Client**

| Field | Type | Required | Description |
|---|---|---|---|
| `client_ts_echo` | integer | yes | Echo of `client_ts` from `client.ping`. Must be > 0. |
| `server_ts` | integer | yes | Server Unix timestamp in ms. Must be > 0. |

```json
{
  "type": "server.pong",
  "version": 1,
  "payload": {
    "client_ts_echo": 1700000060000,
    "server_ts": 1700000060007
  }
}
```

---

### 7.3 Round lifecycle

#### `server.pairing`

Direction: **Server → Client**  
Sent: Before each round, when the server has computed the pairings.

| Field | Type | Required | Description |
|---|---|---|---|
| `round_number` | integer | yes | Current round number. Range: 1–6. |
| `room_id` | string | yes | Unique opaque identifier for this room / pairing. Non-empty. |
| `partner_id` | string | yes | The partner's `participant_id`. Non-empty. |
| `round_start_ts` | integer | yes | Server Unix ms timestamp when the round will start. Must be > 0. |
| `round_end_ts` | integer | yes | Server Unix ms timestamp when the round will end. Must be > 0. |
| `partner_display_name` | string | no | Partner's display name. May be absent if not available. |
| `partner_tags` | string[] | no | Partner's interest tags. May be absent or an empty list. |

```json
{
  "type": "server.pairing",
  "version": 1,
  "payload": {
    "round_number": 1,
    "room_id": "room-0042",
    "partner_id": "p-def456",
    "round_start_ts": 1700000120000,
    "round_end_ts": 1700000540000,
    "partner_display_name": "Sara",
    "partner_tags": ["ai", "music"]
  }
}
```

---

#### `client.ready`

Direction: **Client → Server**  
Sent: After the client has received `server.pairing` and is ready to start the round.

| Field | Type | Required | Description |
|---|---|---|---|
| `round_number` | integer | yes | The round the client is confirming readiness for. Range: 1–6. |

```json
{
  "type": "client.ready",
  "version": 1,
  "payload": { "round_number": 1 }
}
```

**Validation errors:** `ERR_INVALID_STATE` if `round_number` does not match the server's current pairing.

---

#### `server.round_start`

Direction: **Server → Client**  
Sent: At the moment the round begins (may be slightly after `round_start_ts` in `server.pairing`).

| Field | Type | Required | Description |
|---|---|---|---|
| `round_number` | integer | yes | Round number. Range: 1–6. |
| `room_id` | string | yes | The room identifier (matches `server.pairing`). Non-empty. |
| `server_ts` | integer | yes | Server Unix timestamp in ms at round start. Must be > 0. |

```json
{
  "type": "server.round_start",
  "version": 1,
  "payload": {
    "round_number": 1,
    "room_id": "room-0042",
    "server_ts": 1700000120000
  }
}
```

---

#### `server.round_end`

Direction: **Server → Client**  
Sent: When the round ends. Clients MUST stop WebRTC media and clear signalling state.

| Field | Type | Required | Description |
|---|---|---|---|
| `round_number` | integer | yes | The round that has ended. Range: 1–6. |
| `server_ts` | integer | yes | Server Unix timestamp in ms at round end. Must be > 0. |

```json
{
  "type": "server.round_end",
  "version": 1,
  "payload": {
    "round_number": 1,
    "server_ts": 1700000540000
  }
}
```

---

### 7.4 Partner state

#### `server.partner_state`

Direction: **Server → Client**  
Sent: Whenever the partner's connection state changes.

| Field | Type | Required | Description |
|---|---|---|---|
| `partner_id` | string | yes | The partner's `participant_id`. Non-empty. |
| `state` | string | yes | One of `"connected"`, `"disconnected"`, `"reconnecting"`. |
| `server_ts` | integer | yes | Server Unix timestamp in ms of the state change. Must be > 0. |

```json
{
  "type": "server.partner_state",
  "version": 1,
  "payload": {
    "partner_id": "p-def456",
    "state": "disconnected",
    "server_ts": 1700000300000
  }
}
```

---

### 7.5 WebRTC signalling

The server acts as a **signalling relay**: it forwards WebRTC messages between
the two peers in a room without interpreting their content. The server does NOT
participate in the WebRTC peer connection itself.

**Offerer determination:** The participant with the lexicographically smaller
`participant_id` is the offerer. This is deterministic and requires no
negotiation.

#### `client.webrtc.offer`

Direction: **Client → Server** (offerer sends; server forwards to partner)

| Field | Type | Required | Description |
|---|---|---|---|
| `room_id` | string | yes | Must match the current room from `server.pairing`. Non-empty. |
| `sdp` | string | yes | SDP offer string. Non-empty. |

```json
{
  "type": "client.webrtc.offer",
  "version": 1,
  "payload": {
    "room_id": "room-0042",
    "sdp": "v=0\r\no=- 46117..."
  }
}
```

---

#### `server.webrtc.offer`

Direction: **Server → Client** (forwarded offer from partner)

| Field | Type | Required | Description |
|---|---|---|---|
| `room_id` | string | yes | The room identifier. Non-empty. |
| `from_participant_id` | string | yes | `participant_id` of the sender. Non-empty. |
| `sdp` | string | yes | SDP offer string. Non-empty. |

```json
{
  "type": "server.webrtc.offer",
  "version": 1,
  "payload": {
    "room_id": "room-0042",
    "from_participant_id": "p-abc123",
    "sdp": "v=0\r\no=- 46117..."
  }
}
```

---

#### `client.webrtc.answer`

Direction: **Client → Server** (answerer sends; server forwards to partner)

| Field | Type | Required | Description |
|---|---|---|---|
| `room_id` | string | yes | Must match the current room. Non-empty. |
| `sdp` | string | yes | SDP answer string. Non-empty. |

```json
{
  "type": "client.webrtc.answer",
  "version": 1,
  "payload": {
    "room_id": "room-0042",
    "sdp": "v=0\r\no=- 78923..."
  }
}
```

---

#### `server.webrtc.answer`

Direction: **Server → Client** (forwarded answer from partner)

| Field | Type | Required | Description |
|---|---|---|---|
| `room_id` | string | yes | Non-empty. |
| `from_participant_id` | string | yes | `participant_id` of the sender. Non-empty. |
| `sdp` | string | yes | SDP answer string. Non-empty. |

---

#### `client.webrtc.ice_candidate`

Direction: **Client → Server** (trickle ICE — both peers send these)

| Field | Type | Required | Description |
|---|---|---|---|
| `room_id` | string | yes | Must match the current room. Non-empty. |
| `candidate` | string | yes | ICE candidate string. Non-empty. |
| `sdp_mid` | string | yes | Media stream identifier from the SDP. Non-empty. |
| `sdp_mline_index` | integer | yes | Zero-based index of the m= line. Must be ≥ 0. |

```json
{
  "type": "client.webrtc.ice_candidate",
  "version": 1,
  "payload": {
    "room_id": "room-0042",
    "candidate": "candidate:1 1 UDP 2130706431 192.168.1.2 54321 typ host",
    "sdp_mid": "0",
    "sdp_mline_index": 0
  }
}
```

---

#### `server.webrtc.ice_candidate`

Direction: **Server → Client** (forwarded ICE candidate from partner)

| Field | Type | Required | Description |
|---|---|---|---|
| `room_id` | string | yes | Non-empty. |
| `from_participant_id` | string | yes | `participant_id` of the sender. Non-empty. |
| `candidate` | string | yes | ICE candidate string. Non-empty. |
| `sdp_mid` | string | yes | Non-empty. |
| `sdp_mline_index` | integer | yes | Must be ≥ 0. |

---

### 7.6 TURN credentials

#### `server.turn_credentials`

Direction: **Server → Client**  
Sent: After `server.pairing` when the server determines TURN may be needed
(or unconditionally, for simplicity). Credentials are short-lived.

| Field | Type | Required | Description |
|---|---|---|---|
| `urls` | string[] | yes | Non-empty list of TURN/STUN URL strings (e.g. `"turn:relay.example.com:3478"`). |
| `username` | string | yes | TURN username. Non-empty. |
| `credential` | string | yes | TURN credential (password or HMAC token). Non-empty. |
| `ttl` | integer | yes | Seconds until credentials expire. Must be > 0. |

```json
{
  "type": "server.turn_credentials",
  "version": 1,
  "payload": {
    "urls": ["turn:relay.example.com:3478", "turns:relay.example.com:5349"],
    "username": "1700000120:p-abc123",
    "credential": "xK9m2pLqR8...",
    "ttl": 86400
  }
}
```

---

### 7.7 Event end

#### `server.event_end`

Direction: **Server → Client**  
Sent: After the final round ends (or if the event is cancelled/errors).
Clients MUST close the WebSocket gracefully after receiving this.

| Field | Type | Required | Description |
|---|---|---|---|
| `reason` | string | yes | One of `"completed"`, `"cancelled"`, `"error"`. |
| `server_ts` | integer | yes | Server Unix timestamp in ms. Must be > 0. |
| `message` | string | no | Human-readable message (e.g. reason for cancellation). |

```json
{
  "type": "server.event_end",
  "version": 1,
  "payload": {
    "reason": "completed",
    "server_ts": 1700002640000,
    "message": "Thank you for participating!"
  }
}
```

---

### 7.8 Errors

#### `server.error`

Direction: **Server → Client**  
Sent: Whenever a client message is invalid or causes a server-side error. The
WebSocket connection remains open unless the error is fatal (see below).

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | string | yes | Error code from §8. Non-empty. |
| `message` | string | yes | Human-readable description. Non-empty. |
| `original_type` | string | no | The `"type"` of the message that triggered the error, if it could be extracted. |
| `detail` | object | no | Structured context (field name, received value, etc.). |

```json
{
  "type": "server.error",
  "version": 1,
  "payload": {
    "code": "ERR_INVALID_MESSAGE",
    "message": "Missing required field: 'participant_token'",
    "original_type": "client.hello",
    "detail": { "field": "participant_token" }
  }
}
```

**Fatal errors** (server closes the connection after sending `server.error`):
- `ERR_VERSION_MISMATCH` — close code 4000
- `ERR_NOT_AUTHENTICATED` — close code 4001
- `ERR_ALREADY_CONNECTED` — close code 4002

**Non-fatal errors** (connection remains open):
- All others: `ERR_INVALID_JSON`, `ERR_INVALID_MESSAGE`, `ERR_UNKNOWN_TYPE`,
  `ERR_INVALID_STATE`, `ERR_WRONG_ROOM`, `ERR_RATE_LIMITED`

**Server crash guard:** The server MUST NOT let a `ProtocolError` propagate
to the WebSocket handler. Always wrap the receive loop in a try/except and
call `build_server_error` / `error_from_protocol_error`.

---

## 8. Error codes

All error codes follow the `ERR_<SCREAMING_SNAKE>` convention. Define new
codes in `apps/protocol/constants.py` — never hard-code strings in handlers.

| Code | Meaning | Fatal? |
|---|---|---|
| `ERR_INVALID_JSON` | Raw message is not valid JSON | No |
| `ERR_INVALID_MESSAGE` | Valid JSON but schema violation (missing field, wrong type) | No |
| `ERR_UNKNOWN_TYPE` | Unrecognised `"type"` value | No |
| `ERR_VERSION_MISMATCH` | Protocol version not supported | **Yes** — close 4000 |
| `ERR_NOT_AUTHENTICATED` | Token invalid or expired | **Yes** — close 4001 |
| `ERR_ALREADY_CONNECTED` | Participant already has an active WebSocket | **Yes** — close 4002 |
| `ERR_INVALID_STATE` | Message not valid in current session state | No |
| `ERR_WRONG_ROOM` | `room_id` does not match participant's room | No |
| `ERR_RATE_LIMITED` | Too many messages from this client | No |
| `ERR_INTERNAL` | Unexpected internal server error | No |

---

## 9. Field type reference

| Type | Python | JSON | Notes |
|---|---|---|---|
| string | `str` | string | Non-empty unless stated |
| integer | `int` | number (no decimal) | No booleans accepted as integers |
| integer (ts) | `int` | number (no decimal) | Unix time in **milliseconds**; must be > 0 |
| boolean | `bool` | true / false | Distinct from integer |
| string[] | `list[str]` | array of strings | |
| integer[] | `list[int]` | array of numbers | |
| object | `dict` | object | |

---

## 10. Validation rules

The server MUST apply the following checks in order for every inbound message:

1. **JSON parse** — if it fails → `ERR_INVALID_JSON`.
2. **Top-level type** — the parsed value must be a JSON object → `ERR_INVALID_MESSAGE`.
3. **Envelope `version`** — must be present, must be an integer (not bool),
   must be in `SUPPORTED_VERSIONS` → `ERR_INVALID_MESSAGE` / `ERR_VERSION_MISMATCH`.
4. **Envelope `type`** — must be present, must be a string, must be a known
   message type → `ERR_INVALID_MESSAGE` / `ERR_UNKNOWN_TYPE`.
5. **Envelope `payload`** — must be present and a JSON object → `ERR_INVALID_MESSAGE`.
6. **Payload schema** — per-type required/optional field checks (see §7) →
   `ERR_INVALID_MESSAGE`.
7. **Business logic** — state checks (e.g. right room, correct round) →
   `ERR_INVALID_STATE` / `ERR_WRONG_ROOM`. *(Implemented in the handler, not
   the protocol module.)*

Steps 1–6 are handled by `validate_message()` in `apps/protocol/validators.py`.

---

## 11. Extensibility

### Adding a new message type (non-breaking)

1. Add a new member to `MessageType` in `constants.py`.
2. Add a payload validator in `validators.py` and register it in
   `_PAYLOAD_VALIDATORS`.
3. Add a builder in `schemas.py`.
4. Document the message in this file.
5. Add tests in `tests/test_protocol.py`.

No version bump is needed for additions.

### Breaking changes (bump `PROTOCOL_VERSION`)

A change is breaking if:
- A required field is removed or renamed.
- A field's type changes in a non-compatible way.
- A message type is removed.
- The meaning of an existing field changes.

Steps for a breaking change:
1. Increment `PROTOCOL_VERSION` in `constants.py`.
2. Add the new version to `SUPPORTED_VERSIONS`.
3. Keep the old version in `SUPPORTED_VERSIONS` for a defined deprecation window.
4. Update validators and builders.
5. Document the migration path in this file.

---

## 12. Developer guide

### Using constants

```python
from apps.protocol.constants import MessageType, ErrorCode, PROTOCOL_VERSION
```

Never hard-code message type strings or error code strings in your handlers.

### Validating inbound messages

```python
import json
from apps.protocol.validators import validate_message
from apps.protocol.exceptions import ProtocolError
from apps.protocol.schemas import error_from_protocol_error


async def handle_message(ws, raw_text: str) -> None:
    try:
        msg_type, payload = validate_message(raw_text)
    except ProtocolError as exc:
        await ws.send(json.dumps(error_from_protocol_error(exc)))
        return
    # dispatch on msg_type ...
```

### Building outbound messages

```python
from apps.protocol.schemas import (
    build_server_hello,
    build_server_pairing,
    build_server_error,
)

msg = build_server_hello(
    participant_id="p-abc123",
    server_ts=1700000001000,
    client_ts_echo=1700000000000,
    event_id="evt-xyz987",
)
await ws.send(json.dumps(msg))
```

### Error handling pattern

```python
from apps.protocol.exceptions import ProtocolError
from apps.protocol.constants import ErrorCode
from apps.protocol.schemas import build_server_error
import json

# In a WebSocket handler:
try:
    msg_type, payload = validate_message(raw)
except ProtocolError as exc:
    await ws.send(json.dumps(error_from_protocol_error(exc)))
    if exc.code in (
        ErrorCode.ERR_VERSION_MISMATCH,
        ErrorCode.ERR_NOT_AUTHENTICATED,
        ErrorCode.ERR_ALREADY_CONNECTED,
    ):
        await ws.close(4000)  # use appropriate code per §7.8
    return
```

### Adding a new error code

Add it to the `ErrorCode` enum in `constants.py` and document it in §8 of this
spec. Never use a bare string.

---

## 13. Open questions for Ali

The following decisions are unresolved and require Ali's review before
implementation:

1. **Token type:** What format is `participant_token`? JWT (and which
   algorithm), signed session cookie, or opaque token? The validator currently
   checks only that it is a non-empty string; the server-side authentication
   check is not yet specified.

2. **Offerer selection:** This spec proposes the participant with the
   lexicographically smaller `participant_id` is the WebRTC offerer. Is this
   acceptable, or should the server explicitly designate the offerer (e.g. an
   `"is_offerer"` boolean in `server.pairing`)?

3. **TURN policy:** Should `server.turn_credentials` be sent to all
   participants unconditionally, or only when the server detects a participant
   may need TURN? If conditional, what is the detection mechanism?

4. **`client.ready` requirement:** Is the `client.ready` message required (the
   server waits for it before sending `server.round_start`), or is it
   informational (the round starts at `round_start_ts` regardless)?

5. **Reconnection:** If a participant disconnects mid-round and reconnects,
   should `server.pairing` be re-sent, or should the client restore state from
   `server.hello`? What is the reconnect window (seconds)?

6. **Max participants per event:** The spec targets ~900 concurrent
   participants. Should the protocol include an event-capacity field in
   `server.hello` or `server.pairing`?

7. **Rate limiting:** What is the allowed message rate per client? (Default
   assumption: 60 messages/minute.)

8. **WebSocket close codes:** Are the proposed close codes (4000, 4001, 4002)
   acceptable, or should a different range be used?

9. **`server.error` for server-originated messages:** Currently, only inbound
   (client) messages are validated. Should the server also validate outbound
   messages before sending? (Builders already call `validate_payload`, so this
   is partially covered.)
