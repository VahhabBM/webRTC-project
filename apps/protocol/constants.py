"""
Protocol constants: version, message types, directions, and error codes.

Naming conventions
------------------
* MessageType members follow the pattern ``<direction>.<domain>[.<action>]``.
  - Client-originated messages start with ``client.``
  - Server-originated messages start with ``server.``
* ErrorCode members follow the pattern ``ERR_<SCREAMING_SNAKE>``.
* The current protocol version is ``PROTOCOL_VERSION``.  Any future breaking
  change MUST increment this integer and document the migration path.
"""

from enum import StrEnum

# ---------------------------------------------------------------------------
# Protocol version
# ---------------------------------------------------------------------------

#: Increment when a breaking change is introduced.
#: Non-breaking additions (new optional fields, new message types) do NOT
#: require a version bump; instead add them to SUPPORTED_VERSIONS when ready.
PROTOCOL_VERSION: int = 1

#: All versions this server accepts from clients in ``client.hello``.
SUPPORTED_VERSIONS: frozenset[int] = frozenset({1})

# T-14 configuration defaults. Handlers may override these from deployment or
# event configuration; the protocol module only defines the contract defaults.
DEFAULT_RECONNECT_WINDOW_SECONDS: int = 5 * 60
DEFAULT_RATE_LIMIT_MESSAGES_PER_MINUTE: int = 60

# RFC 6455 standard close codes plus private-use application codes.
CLOSE_NORMAL = 1000
CLOSE_MESSAGE_TOO_BIG = 1009
CLOSE_AUTHENTICATION_FAILED = 4001
CLOSE_VERSION_MISMATCH = 4002
CLOSE_POLICY_VIOLATION = 4003
CLOSE_INTERNAL_ERROR = 4004


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


class MessageType(StrEnum):
    """Unique string identifiers for every protocol message.

    Each value is used verbatim as the ``"type"`` field of a message envelope.
    """

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    #: Client → Server.  First message after the WebSocket connection opens.
    CLIENT_HELLO = "client.hello"

    #: Server → Client.  Server acknowledges the connection and confirms the
    #: negotiated protocol version.
    SERVER_HELLO = "server.hello"

    # ------------------------------------------------------------------
    # Clock synchronisation
    # ------------------------------------------------------------------

    #: Client → Server.  Sent at any time to measure round-trip latency and
    #: estimate the server clock offset.
    CLIENT_CLOCK_SYNC = "client.clock_sync"

    #: Server → Client.  Response to ``client.clock_sync``.
    SERVER_CLOCK_SYNC = "server.clock_sync"

    # ------------------------------------------------------------------
    # Keepalive
    # ------------------------------------------------------------------

    #: Client → Server.  Application-level keepalive (separate from the
    #: WebSocket-level ping frame).
    CLIENT_PING = "client.ping"

    #: Server → Client.  Response to ``client.ping``.
    SERVER_PONG = "server.pong"

    # ------------------------------------------------------------------
    # Round lifecycle
    # ------------------------------------------------------------------

    #: Server → Client.  Tells the client who its partner is for the coming
    #: round and when that round starts / ends.
    SERVER_PAIRING = "server.pairing"

    #: Client → Server.  Client signals it is ready to begin the round.
    CLIENT_READY = "client.ready"

    #: Server → Client.  Authoritative signal that a round has started.
    SERVER_ROUND_START = "server.round_start"

    #: Server → Client.  Authoritative signal that a round has ended.
    SERVER_ROUND_END = "server.round_end"

    # ------------------------------------------------------------------
    # Partner state
    # ------------------------------------------------------------------

    #: Server → Client.  Informs the client of the partner's connection state.
    SERVER_PARTNER_STATE = "server.partner_state"

    # ------------------------------------------------------------------
    # WebRTC signalling  (forwarded by the server; not interpreted)
    # ------------------------------------------------------------------

    #: Client → Server.  WebRTC offer SDP (offerer sends to the server, which
    #: forwards it to the partner).
    CLIENT_WEBRTC_OFFER = "client.webrtc.offer"

    #: Server → Client.  Forwarded WebRTC offer from the partner.
    SERVER_WEBRTC_OFFER = "server.webrtc.offer"

    #: Client → Server.  WebRTC answer SDP.
    CLIENT_WEBRTC_ANSWER = "client.webrtc.answer"

    #: Server → Client.  Forwarded WebRTC answer from the partner.
    SERVER_WEBRTC_ANSWER = "server.webrtc.answer"

    #: Client → Server.  Trickle ICE candidate.
    CLIENT_WEBRTC_ICE = "client.webrtc.ice_candidate"

    #: Server → Client.  Forwarded ICE candidate from the partner.
    SERVER_WEBRTC_ICE = "server.webrtc.ice_candidate"

    # ------------------------------------------------------------------
    # TURN credentials
    # ------------------------------------------------------------------

    #: Server → Client.  Delivers short-lived TURN credentials.  Sent after
    #: ``server.pairing`` when the server determines the participant may need
    #: TURN relay (or always, for safety).
    SERVER_TURN_CREDENTIALS = "server.turn_credentials"

    # ------------------------------------------------------------------
    # Event end
    # ------------------------------------------------------------------

    #: Server → Client.  Sent to all participants when the event concludes.
    SERVER_EVENT_END = "server.event_end"

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    #: Server → Client.  Structured error response.  The server MUST send
    #: this (not crash) for any invalid or unexpected client message.
    SERVER_ERROR = "server.error"


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


class ErrorCode(StrEnum):
    """Error codes used in ``server.error`` payloads.

    Every code follows the ``ERR_<SCREAMING_SNAKE>`` pattern.
    Add new codes here; never hard-code strings elsewhere.
    """

    #: The raw text received from the client is not valid JSON.
    ERR_INVALID_JSON = "ERR_INVALID_JSON"

    #: The message is valid JSON but is missing required fields, contains
    #: fields of the wrong type, or otherwise violates the message schema.
    ERR_INVALID_MESSAGE = "ERR_INVALID_MESSAGE"

    #: The ``"type"`` field contains an unrecognised message type.
    ERR_UNKNOWN_TYPE = "ERR_UNKNOWN_TYPE"

    #: The ``"version"`` field contains a protocol version the server does
    #: not support.
    ERR_VERSION_MISMATCH = "ERR_VERSION_MISMATCH"

    #: The participant token in ``client.hello`` is invalid or has expired.
    ERR_NOT_AUTHENTICATED = "ERR_NOT_AUTHENTICATED"

    #: The same participant is already connected on another WebSocket.
    ERR_ALREADY_CONNECTED = "ERR_ALREADY_CONNECTED"

    #: The message is structurally valid but is not legal in the current
    #: session state (e.g. sending a WebRTC offer before receiving a pairing).
    ERR_INVALID_STATE = "ERR_INVALID_STATE"

    #: The ``room_id`` in the message does not match the participant's
    #: currently assigned room.
    ERR_WRONG_ROOM = "ERR_WRONG_ROOM"

    #: The client is sending messages faster than the server allows.
    ERR_RATE_LIMITED = "ERR_RATE_LIMITED"

    #: An unexpected internal error occurred.  The client should not retry
    #: immediately; details are logged server-side.
    ERR_INTERNAL = "ERR_INTERNAL"


ERROR_CLOSE_CODES: dict[ErrorCode, int] = {
    ErrorCode.ERR_VERSION_MISMATCH: CLOSE_VERSION_MISMATCH,
    ErrorCode.ERR_NOT_AUTHENTICATED: CLOSE_AUTHENTICATION_FAILED,
    ErrorCode.ERR_ALREADY_CONNECTED: CLOSE_AUTHENTICATION_FAILED,
    ErrorCode.ERR_RATE_LIMITED: CLOSE_POLICY_VIOLATION,
    ErrorCode.ERR_INTERNAL: CLOSE_INTERNAL_ERROR,
}


# ---------------------------------------------------------------------------
# Partner states
# ---------------------------------------------------------------------------


class PartnerState(StrEnum):
    """Possible values for the ``state`` field of ``server.partner_state``."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"


# ---------------------------------------------------------------------------
# Event-end reasons
# ---------------------------------------------------------------------------


class EventEndReason(StrEnum):
    """Possible values for the ``reason`` field of ``server.event_end``."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"
