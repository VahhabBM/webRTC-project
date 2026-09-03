# Authenticated WebSockets (T-14)

The ASGI application exposes `ws/events/` (the full endpoint is
`/ws/events/`). It uses Channels' `AuthMiddlewareStack`, which loads the
Django session cookie before the consumer runs. The consumer calls
`apps.events.auth.resolve_participant_from_scope(scope)`; the returned
`Participant` is authoritative. No token, header, or client-supplied
`participant_id` is accepted as authentication.

After the socket is accepted, the first application message must be the
T-13 `client.hello` envelope. Its `client_ts` is echoed in `server.hello`,
which also contains the server-resolved participant UUID and event UUID.
Before this handshake only `client.hello` is legal. Afterward, T-14 handles
`client.ping`/`server.pong` and `client.clock_sync`/`server.clock_sync`;
pairing, rounds, and WebRTC messages are reserved for later tasks.

Missing or invalid sessions receive `server.error` with `ERR_NOT_AUTHENTICATED`
and close code 4001. Protocol validation uses the shared T-13 validators and
builders. Fatal version, authentication, and rate-limit errors use the
documented 4002, 4001, and 4003 close codes. Unexpected failures are logged
without exposing internals and use `ERR_INTERNAL`/4004.

Multiple simultaneous connections for one participant are allowed. Every
consumer instance owns its handshake, rate-limit window, activity timestamp,
and heartbeat task; disconnecting one socket cannot affect another participant
or another socket for the same participant.

Defaults are configurable in Django settings:

- `WEBSOCKET_HEARTBEAT_INTERVAL_SECONDS = 30`
- `WEBSOCKET_HEARTBEAT_TIMEOUT_SECONDS = 90`
- `WEBSOCKET_MAX_MESSAGE_BYTES = 65536`
- `PROTOCOL_RATE_LIMIT_MESSAGES_PER_MINUTE = 60`

The heartbeat monitor closes an idle/dead socket after the configured timeout.
Application pings are answered with `server.pong`; WebSocket transport control
frames remain distinct from protocol messages. Clients should send
`client.ping` regularly (the 30-second heartbeat interval is the recommended
cadence) so inbound activity remains below the 90-second timeout. A future
client should reconnect with exponential backoff; the server performs a fresh
session lookup and does not retain stale connection state.
