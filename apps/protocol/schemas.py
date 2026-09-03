"""
Message builder helpers.

``build_message`` constructs a valid, serialisable protocol message dict.
All builders validate their arguments via the same validators used by the
inbound path, so malformed outbound messages are caught at construction time.

Usage
-----
::

    import json
    from apps.protocol.schemas import build_message
    from apps.protocol.constants import MessageType

    msg = build_message(
        MessageType.SERVER_HELLO,
        participant_id="p-abc123",
        server_ts=1_700_000_000_000,
        client_ts_echo=1_699_999_999_000,
        event_id="evt-xyz",
        supported_versions=[1],
    )
    await websocket.send(json.dumps(msg))

Each helper only accepts keyword arguments that match the message schema; any
extra keys are silently accepted to support forward compatibility — the sender
is responsible for keeping payloads clean.
"""

from __future__ import annotations

from .constants import PROTOCOL_VERSION, MessageType
from .validators import validate_payload


def build_message(msg_type: MessageType | str, **payload_fields) -> dict:
    """Build and validate a protocol message envelope.

    Parameters
    ----------
    msg_type:
        The :class:`~apps.protocol.constants.MessageType` for this message.
    **payload_fields:
        Keyword arguments that become the ``payload`` object.

    Returns
    -------
    dict
        A ready-to-serialise message dict with ``type``, ``version``, and
        ``payload`` keys.

    Raises
    ------
    ProtocolError
        If the constructed payload fails schema validation.
    """
    payload = dict(payload_fields)
    validate_payload(msg_type, payload)
    return {
        "type": str(msg_type),
        "version": PROTOCOL_VERSION,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# Typed convenience builders
# These make call-sites readable and IDE-friendly.
# ---------------------------------------------------------------------------


def build_server_hello(
    *,
    participant_id: str,
    server_ts: int,
    client_ts_echo: int,
    event_id: str,
    supported_versions: list[int] | None = None,
    max_participants: int | None = None,
) -> dict:
    """Build a ``server.hello`` message."""
    if supported_versions is None:
        from .constants import SUPPORTED_VERSIONS

        supported_versions = sorted(SUPPORTED_VERSIONS)
    payload = dict(
        participant_id=participant_id,
        server_ts=server_ts,
        client_ts_echo=client_ts_echo,
        event_id=event_id,
        supported_versions=supported_versions,
    )
    if max_participants is not None:
        payload["capacity"] = {"max_participants": max_participants}
    return build_message(MessageType.SERVER_HELLO, **payload)


def build_server_clock_sync(*, client_ts_echo: int, server_ts: int) -> dict:
    """Build a ``server.clock_sync`` message."""
    return build_message(
        MessageType.SERVER_CLOCK_SYNC,
        client_ts_echo=client_ts_echo,
        server_ts=server_ts,
    )


def build_server_pong(*, client_ts_echo: int, server_ts: int) -> dict:
    """Build a ``server.pong`` message."""
    return build_message(
        MessageType.SERVER_PONG,
        client_ts_echo=client_ts_echo,
        server_ts=server_ts,
    )


def build_server_pairing(
    *,
    round_number: int,
    room_id: str,
    partner_id: str,
    round_start_ts: int,
    round_end_ts: int,
    is_offerer: bool = False,
    partner_display_name: str | None = None,
    partner_tags: list[str] | None = None,
) -> dict:
    """Build a ``server.pairing`` message."""
    payload: dict = dict(
        round_number=round_number,
        room_id=room_id,
        partner_id=partner_id,
        round_start_ts=round_start_ts,
        round_end_ts=round_end_ts,
        is_offerer=is_offerer,
    )
    if partner_display_name is not None:
        payload["partner_display_name"] = partner_display_name
    if partner_tags is not None:
        payload["partner_tags"] = partner_tags
    return build_message(MessageType.SERVER_PAIRING, **payload)


def build_server_round_start(
    *, round_number: int, room_id: str, server_ts: int
) -> dict:
    """Build a ``server.round_start`` message."""
    return build_message(
        MessageType.SERVER_ROUND_START,
        round_number=round_number,
        room_id=room_id,
        server_ts=server_ts,
    )


def build_server_round_end(*, round_number: int, server_ts: int) -> dict:
    """Build a ``server.round_end`` message."""
    return build_message(
        MessageType.SERVER_ROUND_END,
        round_number=round_number,
        server_ts=server_ts,
    )


def build_server_partner_state(*, partner_id: str, state: str, server_ts: int) -> dict:
    """Build a ``server.partner_state`` message."""
    return build_message(
        MessageType.SERVER_PARTNER_STATE,
        partner_id=partner_id,
        state=state,
        server_ts=server_ts,
    )


def build_server_webrtc_offer(
    *, room_id: str, from_participant_id: str, sdp: str
) -> dict:
    """Build a ``server.webrtc.offer`` message."""
    return build_message(
        MessageType.SERVER_WEBRTC_OFFER,
        room_id=room_id,
        from_participant_id=from_participant_id,
        sdp=sdp,
    )


def build_server_webrtc_answer(
    *, room_id: str, from_participant_id: str, sdp: str
) -> dict:
    """Build a ``server.webrtc.answer`` message."""
    return build_message(
        MessageType.SERVER_WEBRTC_ANSWER,
        room_id=room_id,
        from_participant_id=from_participant_id,
        sdp=sdp,
    )


def build_server_ice_candidate(
    *,
    room_id: str,
    from_participant_id: str,
    candidate: str,
    sdp_mid: str,
    sdp_mline_index: int,
) -> dict:
    """Build a ``server.webrtc.ice_candidate`` message."""
    return build_message(
        MessageType.SERVER_WEBRTC_ICE,
        room_id=room_id,
        from_participant_id=from_participant_id,
        candidate=candidate,
        sdp_mid=sdp_mid,
        sdp_mline_index=sdp_mline_index,
    )


#: Alias for ``build_server_ice_candidate`` — prefer the full name.
build_server_webrtc_ice = build_server_ice_candidate


def build_server_turn_credentials(
    *, urls: list[str], username: str, credential: str, ttl: int
) -> dict:
    """Build a ``server.turn_credentials`` message."""
    return build_message(
        MessageType.SERVER_TURN_CREDENTIALS,
        urls=urls,
        username=username,
        credential=credential,
        ttl=ttl,
    )


def build_server_event_end(
    *, reason: str, server_ts: int, message: str | None = None
) -> dict:
    """Build a ``server.event_end`` message."""
    payload: dict = dict(reason=reason, server_ts=server_ts)
    if message is not None:
        payload["message"] = message
    return build_message(MessageType.SERVER_EVENT_END, **payload)


def build_server_error(
    *,
    code: str,
    message: str,
    original_type: str | None = None,
    detail: dict | None = None,
) -> dict:
    """Build a ``server.error`` message.

    This is the standard way to reject an invalid client message without
    crashing the connection.
    """
    payload: dict = dict(code=code, message=message)
    if original_type is not None:
        payload["original_type"] = original_type
    if detail is not None:
        payload["detail"] = detail
    return build_message(MessageType.SERVER_ERROR, **payload)


def error_from_protocol_error(exc: ProtocolError) -> dict:  # noqa: F821
    """Convenience: turn a :class:`~apps.protocol.exceptions.ProtocolError`
    into a ready-to-send ``server.error`` message dict."""
    from .exceptions import ProtocolError  # local import to avoid circular

    if not isinstance(exc, ProtocolError):
        raise TypeError(f"Expected ProtocolError, got {type(exc).__name__}")
    return build_server_error(
        code=exc.code,
        message=exc.message,
        original_type=exc.original_type,
        detail=exc.detail if exc.detail else None,
    )
