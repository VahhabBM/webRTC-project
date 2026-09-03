# Participant personal join links (T-08)

## Flow

An event operator issues a link through `issue_join_token(participant)`. The
returned link is `/join/<token>/` (or `PARTICIPANT_JOIN_BASE_URL` plus that path).
The raw token exists only in the caller's memory while the link is delivered.
`GET /join/<token>/` verifies it and establishes the participant session.
`GET /participant/me/` demonstrates the current authenticated identity.

Tokens use the `p1_` prefix followed by `secrets.token_urlsafe(32)`: 32 random
bytes (256 bits; normally 43 URL-safe characters). The database stores a
SHA-256 digest for lookup and a separate Django salted password hash for
verification. Neither field contains the raw token. Lookup is digest-first,
then constant-time Django hash verification.

Tokens are reusable until their server-side `join_token_expires_at`. New tokens
expire at the event's calculated end (`start_time + all round durations +
breaks`); expiration is never accepted from the client. A future resend rotates
the token and invalidates the previous one. Existing records with no expiry
remain valid until rotated, allowing a safe migration of T-04 data.

## Sessions and WebSockets

Django's database-backed session stores only the participant UUID under
`participant_id`; the raw token is not stored in the session. Sessions persist
for 30 days, including browser restart (`SESSION_EXPIRE_AT_BROWSER_CLOSE=False`),
and use HttpOnly, SameSite=Lax cookies. Production settings enable Secure
cookies. HTTP code resolves identity with
`resolve_participant_from_session(request.session)`.

T-14 should wrap its ASGI WebSocket route with Django session middleware and call
`resolve_participant_from_scope(scope)`. It returns the authoritative
`Participant` or `None`; no WebSocket token parsing or duplicate authentication
logic is needed.

Malformed/unknown tokens return HTTP 400 with `join_token_invalid`; expired
tokens return HTTP 410 with `join_token_expired`; unauthenticated identity
requests return HTTP 401. Responses contain no hashes, stack traces, or tokens.

## Resend and local use

There is no external email provider in this project. `issue_join_token` and
`personal_join_link` are the service boundary for a future email adapter; the
adapter must deliver the returned link without logging it. For local testing,
call the service from a shell or management command and use the returned link.
The Django admin shows only token presence and expiry metadata, never token
material.
