"""
Message validation for the WebRTC Event Platform protocol.

Usage
-----
::

    import json
    from apps.protocol.validators import validate_message
    from apps.protocol.exceptions import ProtocolError

    raw_text = websocket.receive()
    try:
        msg_type, payload = validate_message(raw_text)
    except ProtocolError as exc:
        # Build and send a server.error message back to the client.
        ...

``validate_message`` accepts the raw text received from the WebSocket, parses
it, enforces the envelope rules (version, type), then delegates to the
per-type validator.  On success it returns ``(MessageType, payload_dict)``.
On failure it raises :class:`~apps.protocol.exceptions.ProtocolError`.

The server MUST NOT raise bare exceptions to the WebSocket layer; always catch
``ProtocolError`` and reply with ``server.error``.
"""

from __future__ import annotations

import json
from typing import Any

from .constants import (
    SUPPORTED_VERSIONS,
    ErrorCode,
    EventEndReason,
    MessageType,
    PartnerState,
)
from .exceptions import ProtocolError

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CLIENT_MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        MessageType.CLIENT_HELLO,
        MessageType.CLIENT_CLOCK_SYNC,
        MessageType.CLIENT_PING,
        MessageType.CLIENT_READY,
        MessageType.CLIENT_WEBRTC_OFFER,
        MessageType.CLIENT_WEBRTC_ANSWER,
        MessageType.CLIENT_WEBRTC_ICE,
    }
)

_SERVER_MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        MessageType.SERVER_HELLO,
        MessageType.SERVER_CLOCK_SYNC,
        MessageType.SERVER_PONG,
        MessageType.SERVER_PAIRING,
        MessageType.SERVER_ROUND_START,
        MessageType.SERVER_ROUND_END,
        MessageType.SERVER_PARTNER_STATE,
        MessageType.SERVER_WEBRTC_OFFER,
        MessageType.SERVER_WEBRTC_ANSWER,
        MessageType.SERVER_WEBRTC_ICE,
        MessageType.SERVER_TURN_CREDENTIALS,
        MessageType.SERVER_EVENT_END,
        MessageType.SERVER_ERROR,
    }
)

_ALL_KNOWN_TYPES: frozenset[str] = _CLIENT_MESSAGE_TYPES | _SERVER_MESSAGE_TYPES


def _require(
    payload: dict,
    field: str,
    expected_type: type | tuple[type, ...],
    *,
    original_type: str | None = None,
) -> Any:
    """Return ``payload[field]`` or raise :class:`ProtocolError`.

    Checks presence and type.  ``expected_type`` follows the same rules as
    the second argument to ``isinstance``.
    """
    if field not in payload:
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            f"Missing required field: '{field}'",
            original_type=original_type,
            detail={"field": field},
        )
    value = payload[field]
    # bool is a subclass of int in Python; treat bools as NOT valid ints here.
    if isinstance(value, bool) and int in (
        (expected_type,) if isinstance(expected_type, type) else expected_type
    ):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            f"Field '{field}' must be {expected_type} (got bool)",
            original_type=original_type,
            detail={"field": field, "got": type(value).__name__},
        )
    if not isinstance(value, expected_type):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            f"Field '{field}' must be {expected_type} (got {type(value).__name__})",
            original_type=original_type,
            detail={"field": field, "got": type(value).__name__},
        )
    return value


def _require_str(payload: dict, field: str, *, original_type: str | None = None) -> str:
    return _require(payload, field, str, original_type=original_type)


def _require_int(payload: dict, field: str, *, original_type: str | None = None) -> int:
    return _require(payload, field, int, original_type=original_type)


def _require_list(
    payload: dict,
    field: str,
    *,
    original_type: str | None = None,
) -> list:
    return _require(payload, field, list, original_type=original_type)


def _require_positive_int(
    payload: dict,
    field: str,
    *,
    original_type: str | None = None,
) -> int:
    value = _require_int(payload, field, original_type=original_type)
    if value <= 0:
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            f"Field '{field}' must be a positive integer (got {value})",
            original_type=original_type,
            detail={"field": field, "got": value},
        )
    return value


def _require_nonneg_int(
    payload: dict,
    field: str,
    *,
    original_type: str | None = None,
) -> int:
    value = _require_int(payload, field, original_type=original_type)
    if value < 0:
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            f"Field '{field}' must be >= 0 (got {value})",
            original_type=original_type,
            detail={"field": field, "got": value},
        )
    return value


def _require_nonempty_str(
    payload: dict,
    field: str,
    *,
    original_type: str | None = None,
) -> str:
    value = _require_str(payload, field, original_type=original_type)
    if not value.strip():
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            f"Field '{field}' must not be blank",
            original_type=original_type,
            detail={"field": field},
        )
    return value


# ---------------------------------------------------------------------------
# Per-type payload validators  (client-originated messages only)
# ---------------------------------------------------------------------------


def _validate_client_hello(payload: dict) -> None:
    _require_nonempty_str(
        payload, "participant_token", original_type=MessageType.CLIENT_HELLO
    )
    _require_positive_int(payload, "client_ts", original_type=MessageType.CLIENT_HELLO)


def _validate_client_clock_sync(payload: dict) -> None:
    _require_positive_int(
        payload, "client_ts", original_type=MessageType.CLIENT_CLOCK_SYNC
    )


def _validate_client_ping(payload: dict) -> None:
    _require_positive_int(payload, "client_ts", original_type=MessageType.CLIENT_PING)


def _validate_client_ready(payload: dict) -> None:
    round_number = _require_int(
        payload, "round_number", original_type=MessageType.CLIENT_READY
    )
    if not (1 <= round_number <= 6):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Field 'round_number' must be between 1 and 6",
            original_type=MessageType.CLIENT_READY,
            detail={"field": "round_number", "got": round_number},
        )


def _validate_client_webrtc_offer(payload: dict) -> None:
    _require_nonempty_str(
        payload, "room_id", original_type=MessageType.CLIENT_WEBRTC_OFFER
    )
    _require_nonempty_str(payload, "sdp", original_type=MessageType.CLIENT_WEBRTC_OFFER)


def _validate_client_webrtc_answer(payload: dict) -> None:
    _require_nonempty_str(
        payload, "room_id", original_type=MessageType.CLIENT_WEBRTC_ANSWER
    )
    _require_nonempty_str(
        payload, "sdp", original_type=MessageType.CLIENT_WEBRTC_ANSWER
    )


def _validate_client_webrtc_ice(payload: dict) -> None:
    _require_nonempty_str(
        payload, "room_id", original_type=MessageType.CLIENT_WEBRTC_ICE
    )
    _require_nonempty_str(
        payload, "candidate", original_type=MessageType.CLIENT_WEBRTC_ICE
    )
    _require_nonempty_str(
        payload, "sdp_mid", original_type=MessageType.CLIENT_WEBRTC_ICE
    )
    _require_nonneg_int(
        payload, "sdp_mline_index", original_type=MessageType.CLIENT_WEBRTC_ICE
    )


# ---------------------------------------------------------------------------
# Per-type payload validators  (server-originated messages)
# Server messages are validated here so the *build* helpers can be tested
# against their own schemas.
# ---------------------------------------------------------------------------


def _validate_server_hello(payload: dict) -> None:
    _require_nonempty_str(
        payload, "participant_id", original_type=MessageType.SERVER_HELLO
    )
    _require_positive_int(payload, "server_ts", original_type=MessageType.SERVER_HELLO)
    _require_positive_int(
        payload, "client_ts_echo", original_type=MessageType.SERVER_HELLO
    )
    _require_nonempty_str(payload, "event_id", original_type=MessageType.SERVER_HELLO)
    versions = _require_list(
        payload, "supported_versions", original_type=MessageType.SERVER_HELLO
    )
    if not versions:
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Field 'supported_versions' must not be empty",
            original_type=MessageType.SERVER_HELLO,
        )
    for v in versions:
        if not isinstance(v, int) or isinstance(v, bool):
            raise ProtocolError(
                ErrorCode.ERR_INVALID_MESSAGE,
                "Each element of 'supported_versions' must be an integer",
                original_type=MessageType.SERVER_HELLO,
            )


def _validate_server_clock_sync(payload: dict) -> None:
    _require_positive_int(
        payload, "client_ts_echo", original_type=MessageType.SERVER_CLOCK_SYNC
    )
    _require_positive_int(
        payload, "server_ts", original_type=MessageType.SERVER_CLOCK_SYNC
    )


def _validate_server_pong(payload: dict) -> None:
    _require_positive_int(
        payload, "client_ts_echo", original_type=MessageType.SERVER_PONG
    )
    _require_positive_int(payload, "server_ts", original_type=MessageType.SERVER_PONG)


def _validate_server_pairing(payload: dict) -> None:
    round_number = _require_int(
        payload, "round_number", original_type=MessageType.SERVER_PAIRING
    )
    if not (1 <= round_number <= 6):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Field 'round_number' must be between 1 and 6",
            original_type=MessageType.SERVER_PAIRING,
            detail={"field": "round_number", "got": round_number},
        )
    _require_nonempty_str(payload, "room_id", original_type=MessageType.SERVER_PAIRING)
    _require_nonempty_str(
        payload, "partner_id", original_type=MessageType.SERVER_PAIRING
    )
    _require_positive_int(
        payload, "round_start_ts", original_type=MessageType.SERVER_PAIRING
    )
    _require_positive_int(
        payload, "round_end_ts", original_type=MessageType.SERVER_PAIRING
    )
    # Optional fields: partner_display_name (str), partner_tags (list of str)
    if "partner_display_name" in payload and not isinstance(
        payload["partner_display_name"], str
    ):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Optional field 'partner_display_name' must be a string",
            original_type=MessageType.SERVER_PAIRING,
        )
    if "partner_tags" in payload:
        tags = payload["partner_tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ProtocolError(
                ErrorCode.ERR_INVALID_MESSAGE,
                "Optional field 'partner_tags' must be a list of strings",
                original_type=MessageType.SERVER_PAIRING,
            )


def _validate_server_round_start(payload: dict) -> None:
    round_number = _require_int(
        payload, "round_number", original_type=MessageType.SERVER_ROUND_START
    )
    if not (1 <= round_number <= 6):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Field 'round_number' must be between 1 and 6",
            original_type=MessageType.SERVER_ROUND_START,
            detail={"field": "round_number", "got": round_number},
        )
    _require_nonempty_str(
        payload, "room_id", original_type=MessageType.SERVER_ROUND_START
    )
    _require_positive_int(
        payload, "server_ts", original_type=MessageType.SERVER_ROUND_START
    )


def _validate_server_round_end(payload: dict) -> None:
    round_number = _require_int(
        payload, "round_number", original_type=MessageType.SERVER_ROUND_END
    )
    if not (1 <= round_number <= 6):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Field 'round_number' must be between 1 and 6",
            original_type=MessageType.SERVER_ROUND_END,
            detail={"field": "round_number", "got": round_number},
        )
    _require_positive_int(
        payload, "server_ts", original_type=MessageType.SERVER_ROUND_END
    )


def _validate_server_partner_state(payload: dict) -> None:
    _require_nonempty_str(
        payload, "partner_id", original_type=MessageType.SERVER_PARTNER_STATE
    )
    state = _require_nonempty_str(
        payload, "state", original_type=MessageType.SERVER_PARTNER_STATE
    )
    valid_states = {s.value for s in PartnerState}
    if state not in valid_states:
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            f"Field 'state' must be one of {sorted(valid_states)} (got {state!r})",
            original_type=MessageType.SERVER_PARTNER_STATE,
            detail={"field": "state", "got": state},
        )
    _require_positive_int(
        payload, "server_ts", original_type=MessageType.SERVER_PARTNER_STATE
    )


def _validate_server_webrtc_offer(payload: dict) -> None:
    _require_nonempty_str(
        payload, "room_id", original_type=MessageType.SERVER_WEBRTC_OFFER
    )
    _require_nonempty_str(
        payload, "from_participant_id", original_type=MessageType.SERVER_WEBRTC_OFFER
    )
    _require_nonempty_str(payload, "sdp", original_type=MessageType.SERVER_WEBRTC_OFFER)


def _validate_server_webrtc_answer(payload: dict) -> None:
    _require_nonempty_str(
        payload, "room_id", original_type=MessageType.SERVER_WEBRTC_ANSWER
    )
    _require_nonempty_str(
        payload, "from_participant_id", original_type=MessageType.SERVER_WEBRTC_ANSWER
    )
    _require_nonempty_str(
        payload, "sdp", original_type=MessageType.SERVER_WEBRTC_ANSWER
    )


def _validate_server_webrtc_ice(payload: dict) -> None:
    _require_nonempty_str(
        payload, "room_id", original_type=MessageType.SERVER_WEBRTC_ICE
    )
    _require_nonempty_str(
        payload, "from_participant_id", original_type=MessageType.SERVER_WEBRTC_ICE
    )
    _require_nonempty_str(
        payload, "candidate", original_type=MessageType.SERVER_WEBRTC_ICE
    )
    _require_nonempty_str(
        payload, "sdp_mid", original_type=MessageType.SERVER_WEBRTC_ICE
    )
    _require_nonneg_int(
        payload, "sdp_mline_index", original_type=MessageType.SERVER_WEBRTC_ICE
    )


def _validate_server_turn_credentials(payload: dict) -> None:
    urls = _require_list(
        payload, "urls", original_type=MessageType.SERVER_TURN_CREDENTIALS
    )
    if not urls:
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Field 'urls' must not be empty",
            original_type=MessageType.SERVER_TURN_CREDENTIALS,
        )
    if not all(isinstance(u, str) and u.strip() for u in urls):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Each element of 'urls' must be a non-empty string",
            original_type=MessageType.SERVER_TURN_CREDENTIALS,
        )
    _require_nonempty_str(
        payload, "username", original_type=MessageType.SERVER_TURN_CREDENTIALS
    )
    _require_nonempty_str(
        payload, "credential", original_type=MessageType.SERVER_TURN_CREDENTIALS
    )
    _require_positive_int(
        payload, "ttl", original_type=MessageType.SERVER_TURN_CREDENTIALS
    )


def _validate_server_event_end(payload: dict) -> None:
    reason = _require_nonempty_str(
        payload, "reason", original_type=MessageType.SERVER_EVENT_END
    )
    valid_reasons = {r.value for r in EventEndReason}
    if reason not in valid_reasons:
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            f"Field 'reason' must be one of {sorted(valid_reasons)} (got {reason!r})",
            original_type=MessageType.SERVER_EVENT_END,
            detail={"field": "reason", "got": reason},
        )
    _require_positive_int(
        payload, "server_ts", original_type=MessageType.SERVER_EVENT_END
    )
    # Optional: message (str)
    if "message" in payload and not isinstance(payload["message"], str):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Optional field 'message' must be a string",
            original_type=MessageType.SERVER_EVENT_END,
        )


def _validate_server_error(payload: dict) -> None:
    _require_nonempty_str(payload, "code", original_type=MessageType.SERVER_ERROR)
    _require_nonempty_str(payload, "message", original_type=MessageType.SERVER_ERROR)
    # Optional: original_type (str), detail (dict)
    if "original_type" in payload and not isinstance(payload["original_type"], str):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Optional field 'original_type' must be a string",
            original_type=MessageType.SERVER_ERROR,
        )
    if "detail" in payload and not isinstance(payload["detail"], dict):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Optional field 'detail' must be an object",
            original_type=MessageType.SERVER_ERROR,
        )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_PAYLOAD_VALIDATORS: dict[str, Any] = {
    MessageType.CLIENT_HELLO: _validate_client_hello,
    MessageType.CLIENT_CLOCK_SYNC: _validate_client_clock_sync,
    MessageType.CLIENT_PING: _validate_client_ping,
    MessageType.CLIENT_READY: _validate_client_ready,
    MessageType.CLIENT_WEBRTC_OFFER: _validate_client_webrtc_offer,
    MessageType.CLIENT_WEBRTC_ANSWER: _validate_client_webrtc_answer,
    MessageType.CLIENT_WEBRTC_ICE: _validate_client_webrtc_ice,
    MessageType.SERVER_HELLO: _validate_server_hello,
    MessageType.SERVER_CLOCK_SYNC: _validate_server_clock_sync,
    MessageType.SERVER_PONG: _validate_server_pong,
    MessageType.SERVER_PAIRING: _validate_server_pairing,
    MessageType.SERVER_ROUND_START: _validate_server_round_start,
    MessageType.SERVER_ROUND_END: _validate_server_round_end,
    MessageType.SERVER_PARTNER_STATE: _validate_server_partner_state,
    MessageType.SERVER_WEBRTC_OFFER: _validate_server_webrtc_offer,
    MessageType.SERVER_WEBRTC_ANSWER: _validate_server_webrtc_answer,
    MessageType.SERVER_WEBRTC_ICE: _validate_server_webrtc_ice,
    MessageType.SERVER_TURN_CREDENTIALS: _validate_server_turn_credentials,
    MessageType.SERVER_EVENT_END: _validate_server_event_end,
    MessageType.SERVER_ERROR: _validate_server_error,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_message(raw: str | bytes) -> tuple[MessageType, dict]:
    """Parse and validate a raw WebSocket message.

    Parameters
    ----------
    raw:
        The text (or bytes) received from the WebSocket.

    Returns
    -------
    tuple[MessageType, dict]
        ``(message_type, payload)`` on success.

    Raises
    ------
    ProtocolError
        With an appropriate :class:`~apps.protocol.constants.ErrorCode` on
        any validation failure.  The caller MUST catch this and reply with a
        ``server.error`` message — it must NOT propagate to crash the handler.
    """
    # 1. JSON parse
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError(
            ErrorCode.ERR_INVALID_JSON,
            f"Message is not valid JSON: {exc}",
        ) from exc

    if not isinstance(data, dict):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Message must be a JSON object",
        )

    # 2. Envelope: version
    if "version" not in data:
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Missing required envelope field: 'version'",
        )
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Envelope field 'version' must be an integer",
        )
    if version not in SUPPORTED_VERSIONS:
        raise ProtocolError(
            ErrorCode.ERR_VERSION_MISMATCH,
            f"Protocol version {version} is not supported. "
            f"Supported versions: {sorted(SUPPORTED_VERSIONS)}",
            detail={"received": version, "supported": sorted(SUPPORTED_VERSIONS)},
        )

    # 3. Envelope: type
    if "type" not in data:
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Missing required envelope field: 'type'",
        )
    msg_type = data["type"]
    if not isinstance(msg_type, str):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Envelope field 'type' must be a string",
        )
    if msg_type not in _ALL_KNOWN_TYPES:
        raise ProtocolError(
            ErrorCode.ERR_UNKNOWN_TYPE,
            f"Unknown message type: {msg_type!r}",
            original_type=msg_type,
        )

    # 4. Envelope: payload
    if "payload" not in data:
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Missing required envelope field: 'payload'",
            original_type=msg_type,
        )
    payload = data["payload"]
    if not isinstance(payload, dict):
        raise ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Envelope field 'payload' must be a JSON object",
            original_type=msg_type,
        )

    # 5. Per-type payload validation
    validator = _PAYLOAD_VALIDATORS.get(msg_type)
    if validator is not None:
        validator(payload)

    return MessageType(msg_type), payload


def validate_payload(msg_type: MessageType | str, payload: dict) -> None:
    """Validate a payload dict against the schema for *msg_type*.

    Useful when you have already parsed the envelope and only need to
    re-validate the payload (e.g. when building outbound server messages).

    Raises
    ------
    ProtocolError
        If the payload is invalid for the given type.
    """
    msg_type_str = str(msg_type)
    validator = _PAYLOAD_VALIDATORS.get(msg_type_str)
    if validator is None:
        raise ProtocolError(
            ErrorCode.ERR_UNKNOWN_TYPE,
            f"No validator registered for type: {msg_type_str!r}",
        )
    validator(payload)
