"""
Tests for the WebSocket protocol contract (T-13).

Coverage:
- Constants: MessageType, ErrorCode, PartnerState, EventEndReason
- validate_message(): envelope validation, version negotiation, per-type validation
- validate_payload(): direct payload validation
- Builder helpers in schemas.py
- ProtocolError exception
- error_from_protocol_error helper
- All client-originated message types (valid + invalid)
- All server-originated message types (valid via builders)
- Error codes exhaustiveness
"""

import json

import pytest

from apps.protocol.constants import (
    PROTOCOL_VERSION,
    SUPPORTED_VERSIONS,
    ErrorCode,
    EventEndReason,
    MessageType,
    PartnerState,
)
from apps.protocol.exceptions import ProtocolError
from apps.protocol.schemas import (
    build_message,
    build_server_clock_sync,
    build_server_error,
    build_server_event_end,
    build_server_hello,
    build_server_ice_candidate,
    build_server_pairing,
    build_server_partner_state,
    build_server_pong,
    build_server_round_end,
    build_server_round_start,
    build_server_turn_credentials,
    build_server_webrtc_answer,
    build_server_webrtc_offer,
    error_from_protocol_error,
)
from apps.protocol.validators import validate_message, validate_payload

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap(msg_type: str, payload: dict, version: int = PROTOCOL_VERSION) -> str:
    """Serialise a raw envelope to JSON text."""
    return json.dumps({"type": msg_type, "version": version, "payload": payload})


def _assert_protocol_error(raw: str, code: str) -> ProtocolError:
    """Assert validate_message raises ProtocolError with the given code."""
    with pytest.raises(ProtocolError) as exc_info:
        validate_message(raw)
    assert exc_info.value.code == code, (
        f"Expected error code {code!r}, got {exc_info.value.code!r}: "
        f"{exc_info.value.message}"
    )
    return exc_info.value


# ---------------------------------------------------------------------------
# 1. Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_protocol_version_is_int(self):
        assert isinstance(PROTOCOL_VERSION, int)
        assert PROTOCOL_VERSION >= 1

    def test_supported_versions_contains_current(self):
        assert PROTOCOL_VERSION in SUPPORTED_VERSIONS

    def test_message_type_values_are_strings(self):
        for mt in MessageType:
            assert isinstance(mt.value, str)
            assert mt.value  # non-empty

    def test_client_types_start_with_client(self):
        client_types = [
            MessageType.CLIENT_HELLO,
            MessageType.CLIENT_CLOCK_SYNC,
            MessageType.CLIENT_PING,
            MessageType.CLIENT_READY,
            MessageType.CLIENT_WEBRTC_OFFER,
            MessageType.CLIENT_WEBRTC_ANSWER,
            MessageType.CLIENT_WEBRTC_ICE,
        ]
        for mt in client_types:
            assert mt.value.startswith("client."), (
                f"{mt.value} should start with 'client.'"
            )

    def test_server_types_start_with_server(self):
        server_types = [
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
        ]
        for mt in server_types:
            assert mt.value.startswith("server."), (
                f"{mt.value} should start with 'server.'"
            )

    def test_error_codes_start_with_err(self):
        for code in ErrorCode:
            assert code.value.startswith("ERR_"), (
                f"{code.value} should start with 'ERR_'"
            )

    def test_error_codes_are_unique(self):
        values = [c.value for c in ErrorCode]
        assert len(values) == len(set(values))

    def test_message_types_are_unique(self):
        values = [mt.value for mt in MessageType]
        assert len(values) == len(set(values))

    def test_partner_states(self):
        states = {s.value for s in PartnerState}
        assert states == {"connected", "disconnected", "reconnecting"}

    def test_event_end_reasons(self):
        reasons = {r.value for r in EventEndReason}
        assert reasons == {"completed", "cancelled", "error"}


# ---------------------------------------------------------------------------
# 2. ProtocolError exception
# ---------------------------------------------------------------------------


class TestProtocolError:
    def test_basic_construction(self):
        exc = ProtocolError(ErrorCode.ERR_INVALID_JSON, "bad json")
        assert exc.code == "ERR_INVALID_JSON"
        assert exc.message == "bad json"
        assert exc.original_type is None
        assert exc.detail == {}

    def test_with_all_fields(self):
        exc = ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "missing field",
            original_type="client.hello",
            detail={"field": "participant_token"},
        )
        assert exc.code == "ERR_INVALID_MESSAGE"
        assert exc.original_type == "client.hello"
        assert exc.detail == {"field": "participant_token"}

    def test_is_exception(self):
        exc = ProtocolError(ErrorCode.ERR_INTERNAL, "oops")
        assert isinstance(exc, Exception)

    def test_repr(self):
        exc = ProtocolError(ErrorCode.ERR_INTERNAL, "oops")
        assert "ERR_INTERNAL" in repr(exc)

    def test_str_code_accepted(self):
        exc = ProtocolError("ERR_CUSTOM", "custom error")
        assert exc.code == "ERR_CUSTOM"


# ---------------------------------------------------------------------------
# 3. Envelope validation
# ---------------------------------------------------------------------------


class TestEnvelopeValidation:
    def test_valid_envelope(self):
        raw = _wrap("client.ping", {"client_ts": 1000})
        msg_type, payload = validate_message(raw)
        assert msg_type == MessageType.CLIENT_PING
        assert payload["client_ts"] == 1000

    def test_bytes_input(self):
        raw = _wrap("client.ping", {"client_ts": 1000}).encode()
        msg_type, payload = validate_message(raw)
        assert msg_type == MessageType.CLIENT_PING

    def test_invalid_json(self):
        _assert_protocol_error("not-json{", ErrorCode.ERR_INVALID_JSON)

    def test_empty_string(self):
        _assert_protocol_error("", ErrorCode.ERR_INVALID_JSON)

    def test_json_array_is_rejected(self):
        _assert_protocol_error("[1, 2, 3]", ErrorCode.ERR_INVALID_MESSAGE)

    def test_json_null_is_rejected(self):
        _assert_protocol_error("null", ErrorCode.ERR_INVALID_MESSAGE)

    def test_json_string_is_rejected(self):
        _assert_protocol_error('"hello"', ErrorCode.ERR_INVALID_MESSAGE)

    def test_missing_version(self):
        raw = json.dumps({"type": "client.ping", "payload": {"client_ts": 1000}})
        _assert_protocol_error(raw, ErrorCode.ERR_INVALID_MESSAGE)

    def test_bool_version_rejected(self):
        raw = json.dumps(
            {"type": "client.ping", "version": True, "payload": {"client_ts": 1000}}
        )
        _assert_protocol_error(raw, ErrorCode.ERR_INVALID_MESSAGE)

    def test_string_version_rejected(self):
        raw = json.dumps(
            {"type": "client.ping", "version": "1", "payload": {"client_ts": 1000}}
        )
        _assert_protocol_error(raw, ErrorCode.ERR_INVALID_MESSAGE)

    def test_unsupported_version(self):
        err = _assert_protocol_error(
            json.dumps(
                {"type": "client.ping", "version": 999, "payload": {"client_ts": 1000}}
            ),
            ErrorCode.ERR_VERSION_MISMATCH,
        )
        assert err.detail["received"] == 999

    def test_missing_type(self):
        raw = json.dumps({"version": 1, "payload": {}})
        _assert_protocol_error(raw, ErrorCode.ERR_INVALID_MESSAGE)

    def test_non_string_type(self):
        raw = json.dumps({"type": 42, "version": 1, "payload": {}})
        _assert_protocol_error(raw, ErrorCode.ERR_INVALID_MESSAGE)

    def test_unknown_type(self):
        err = _assert_protocol_error(
            _wrap("completely.unknown", {}),
            ErrorCode.ERR_UNKNOWN_TYPE,
        )
        assert err.original_type == "completely.unknown"

    def test_missing_payload(self):
        raw = json.dumps({"type": "client.ping", "version": 1})
        _assert_protocol_error(raw, ErrorCode.ERR_INVALID_MESSAGE)

    def test_non_object_payload(self):
        raw = json.dumps({"type": "client.ping", "version": 1, "payload": [1, 2]})
        _assert_protocol_error(raw, ErrorCode.ERR_INVALID_MESSAGE)


# ---------------------------------------------------------------------------
# 4. client.hello
# ---------------------------------------------------------------------------


class TestClientHello:
    VALID = {"participant_token": "tok-abc", "client_ts": 1_700_000_000_000}

    def test_valid(self):
        raw = _wrap(MessageType.CLIENT_HELLO, self.VALID)
        msg_type, payload = validate_message(raw)
        assert msg_type == MessageType.CLIENT_HELLO
        assert payload["participant_token"] == "tok-abc"

    def test_missing_token(self):
        bad = {"client_ts": 1_000}
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_HELLO, bad), ErrorCode.ERR_INVALID_MESSAGE
        )

    def test_blank_token(self):
        bad = {"participant_token": "   ", "client_ts": 1_000}
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_HELLO, bad), ErrorCode.ERR_INVALID_MESSAGE
        )

    def test_missing_client_ts(self):
        bad = {"participant_token": "tok"}
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_HELLO, bad), ErrorCode.ERR_INVALID_MESSAGE
        )

    def test_zero_client_ts(self):
        bad = {"participant_token": "tok", "client_ts": 0}
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_HELLO, bad), ErrorCode.ERR_INVALID_MESSAGE
        )

    def test_negative_client_ts(self):
        bad = {"participant_token": "tok", "client_ts": -1}
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_HELLO, bad), ErrorCode.ERR_INVALID_MESSAGE
        )

    def test_bool_client_ts_rejected(self):
        bad = {"participant_token": "tok", "client_ts": True}
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_HELLO, bad), ErrorCode.ERR_INVALID_MESSAGE
        )

    def test_extra_fields_tolerated(self):
        extra = dict(self.VALID, future_field="value")
        raw = _wrap(MessageType.CLIENT_HELLO, extra)
        msg_type, _ = validate_message(raw)
        assert msg_type == MessageType.CLIENT_HELLO


# ---------------------------------------------------------------------------
# 5. client.clock_sync
# ---------------------------------------------------------------------------


class TestClientClockSync:
    def test_valid(self):
        raw = _wrap(MessageType.CLIENT_CLOCK_SYNC, {"client_ts": 1_000_000})
        msg_type, _ = validate_message(raw)
        assert msg_type == MessageType.CLIENT_CLOCK_SYNC

    def test_missing_field(self):
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_CLOCK_SYNC, {}), ErrorCode.ERR_INVALID_MESSAGE
        )

    def test_zero_ts(self):
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_CLOCK_SYNC, {"client_ts": 0}),
            ErrorCode.ERR_INVALID_MESSAGE,
        )


# ---------------------------------------------------------------------------
# 6. client.ping
# ---------------------------------------------------------------------------


class TestClientPing:
    def test_valid(self):
        raw = _wrap(MessageType.CLIENT_PING, {"client_ts": 1_000_000})
        msg_type, _ = validate_message(raw)
        assert msg_type == MessageType.CLIENT_PING

    def test_missing_field(self):
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_PING, {}), ErrorCode.ERR_INVALID_MESSAGE
        )


# ---------------------------------------------------------------------------
# 7. client.ready
# ---------------------------------------------------------------------------


class TestClientReady:
    @pytest.mark.parametrize("round_number", [1, 2, 3, 4, 5, 6])
    def test_valid_round_numbers(self, round_number):
        raw = _wrap(MessageType.CLIENT_READY, {"round_number": round_number})
        msg_type, payload = validate_message(raw)
        assert msg_type == MessageType.CLIENT_READY
        assert payload["round_number"] == round_number

    @pytest.mark.parametrize("bad", [0, 7, -1, 100])
    def test_invalid_round_number(self, bad):
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_READY, {"round_number": bad}),
            ErrorCode.ERR_INVALID_MESSAGE,
        )

    def test_missing_round_number(self):
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_READY, {}), ErrorCode.ERR_INVALID_MESSAGE
        )

    def test_string_round_number(self):
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_READY, {"round_number": "1"}),
            ErrorCode.ERR_INVALID_MESSAGE,
        )


# ---------------------------------------------------------------------------
# 8. client.webrtc.offer
# ---------------------------------------------------------------------------


class TestClientWebRTCOffer:
    VALID = {"room_id": "room-1", "sdp": "v=0\r\no=..."}

    def test_valid(self):
        msg_type, _ = validate_message(
            _wrap(MessageType.CLIENT_WEBRTC_OFFER, self.VALID)
        )
        assert msg_type == MessageType.CLIENT_WEBRTC_OFFER

    def test_missing_room_id(self):
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_WEBRTC_OFFER, {"sdp": "v=0"}),
            ErrorCode.ERR_INVALID_MESSAGE,
        )

    def test_missing_sdp(self):
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_WEBRTC_OFFER, {"room_id": "room-1"}),
            ErrorCode.ERR_INVALID_MESSAGE,
        )

    def test_blank_room_id(self):
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_WEBRTC_OFFER, {"room_id": "  ", "sdp": "v=0"}),
            ErrorCode.ERR_INVALID_MESSAGE,
        )

    def test_blank_sdp(self):
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_WEBRTC_OFFER, {"room_id": "room-1", "sdp": ""}),
            ErrorCode.ERR_INVALID_MESSAGE,
        )


# ---------------------------------------------------------------------------
# 9. client.webrtc.answer
# ---------------------------------------------------------------------------


class TestClientWebRTCAnswer:
    VALID = {"room_id": "room-1", "sdp": "v=0\r\no=answer..."}

    def test_valid(self):
        msg_type, _ = validate_message(
            _wrap(MessageType.CLIENT_WEBRTC_ANSWER, self.VALID)
        )
        assert msg_type == MessageType.CLIENT_WEBRTC_ANSWER

    def test_missing_sdp(self):
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_WEBRTC_ANSWER, {"room_id": "room-1"}),
            ErrorCode.ERR_INVALID_MESSAGE,
        )


# ---------------------------------------------------------------------------
# 10. client.webrtc.ice_candidate
# ---------------------------------------------------------------------------


class TestClientWebRTCIce:
    VALID = {
        "room_id": "room-1",
        "candidate": "candidate:1 1 UDP ...",
        "sdp_mid": "0",
        "sdp_mline_index": 0,
    }

    def test_valid(self):
        msg_type, payload = validate_message(
            _wrap(MessageType.CLIENT_WEBRTC_ICE, self.VALID)
        )
        assert msg_type == MessageType.CLIENT_WEBRTC_ICE
        assert payload["sdp_mline_index"] == 0

    def test_zero_sdp_mline_index_allowed(self):
        raw = _wrap(MessageType.CLIENT_WEBRTC_ICE, self.VALID)
        _, payload = validate_message(raw)
        assert payload["sdp_mline_index"] == 0

    def test_negative_sdp_mline_index(self):
        bad = dict(self.VALID, sdp_mline_index=-1)
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_WEBRTC_ICE, bad),
            ErrorCode.ERR_INVALID_MESSAGE,
        )

    def test_missing_candidate(self):
        bad = {k: v for k, v in self.VALID.items() if k != "candidate"}
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_WEBRTC_ICE, bad),
            ErrorCode.ERR_INVALID_MESSAGE,
        )

    def test_missing_sdp_mid(self):
        bad = {k: v for k, v in self.VALID.items() if k != "sdp_mid"}
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_WEBRTC_ICE, bad),
            ErrorCode.ERR_INVALID_MESSAGE,
        )

    def test_bool_mline_index_rejected(self):
        bad = dict(self.VALID, sdp_mline_index=True)
        _assert_protocol_error(
            _wrap(MessageType.CLIENT_WEBRTC_ICE, bad),
            ErrorCode.ERR_INVALID_MESSAGE,
        )


# ---------------------------------------------------------------------------
# 11. Server message builders (validate on construction)
# ---------------------------------------------------------------------------


class TestServerBuilders:
    def test_server_hello(self):
        msg = build_server_hello(
            participant_id="p-1",
            server_ts=2_000_000,
            client_ts_echo=1_000_000,
            event_id="evt-1",
        )
        assert msg["type"] == MessageType.SERVER_HELLO
        assert msg["version"] == PROTOCOL_VERSION
        assert msg["payload"]["supported_versions"] == [1]

    def test_server_hello_explicit_versions(self):
        msg = build_server_hello(
            participant_id="p-1",
            server_ts=2_000_000,
            client_ts_echo=1_000_000,
            event_id="evt-1",
            supported_versions=[1],
        )
        assert msg["payload"]["supported_versions"] == [1]

    def test_server_hello_rejects_blank_participant_id(self):
        with pytest.raises(ProtocolError):
            build_server_hello(
                participant_id="",
                server_ts=2_000_000,
                client_ts_echo=1_000_000,
                event_id="evt-1",
            )

    def test_server_clock_sync(self):
        msg = build_server_clock_sync(client_ts_echo=1_000, server_ts=2_000)
        assert msg["type"] == MessageType.SERVER_CLOCK_SYNC
        assert msg["payload"]["client_ts_echo"] == 1_000

    def test_server_pong(self):
        msg = build_server_pong(client_ts_echo=1_000, server_ts=2_000)
        assert msg["type"] == MessageType.SERVER_PONG

    def test_server_pairing_minimal(self):
        msg = build_server_pairing(
            round_number=1,
            room_id="room-42",
            partner_id="p-2",
            round_start_ts=5_000,
            round_end_ts=10_000,
        )
        assert msg["type"] == MessageType.SERVER_PAIRING
        assert "partner_display_name" not in msg["payload"]
        assert "partner_tags" not in msg["payload"]

    def test_server_pairing_with_optional_fields(self):
        msg = build_server_pairing(
            round_number=3,
            room_id="room-7",
            partner_id="p-99",
            round_start_ts=5_000,
            round_end_ts=10_000,
            partner_display_name="Sara",
            partner_tags=["ai", "music"],
        )
        assert msg["payload"]["partner_display_name"] == "Sara"
        assert msg["payload"]["partner_tags"] == ["ai", "music"]

    def test_server_pairing_invalid_round(self):
        with pytest.raises(ProtocolError):
            build_server_pairing(
                round_number=7,
                room_id="room-1",
                partner_id="p-2",
                round_start_ts=1,
                round_end_ts=2,
            )

    def test_server_round_start(self):
        msg = build_server_round_start(
            round_number=2, room_id="room-1", server_ts=5_000
        )
        assert msg["type"] == MessageType.SERVER_ROUND_START

    def test_server_round_end(self):
        msg = build_server_round_end(round_number=2, server_ts=10_000)
        assert msg["type"] == MessageType.SERVER_ROUND_END

    def test_server_partner_state_connected(self):
        msg = build_server_partner_state(
            partner_id="p-2", state=PartnerState.CONNECTED, server_ts=5_000
        )
        assert msg["payload"]["state"] == "connected"

    def test_server_partner_state_invalid(self):
        with pytest.raises(ProtocolError):
            build_server_partner_state(
                partner_id="p-2", state="flying", server_ts=5_000
            )

    def test_server_webrtc_offer(self):
        msg = build_server_webrtc_offer(
            room_id="room-1", from_participant_id="p-1", sdp="v=0"
        )
        assert msg["type"] == MessageType.SERVER_WEBRTC_OFFER
        assert msg["payload"]["from_participant_id"] == "p-1"

    def test_server_webrtc_answer(self):
        msg = build_server_webrtc_answer(
            room_id="room-1", from_participant_id="p-1", sdp="v=0"
        )
        assert msg["type"] == MessageType.SERVER_WEBRTC_ANSWER

    def test_server_ice_candidate(self):
        msg = build_server_ice_candidate(
            room_id="room-1",
            from_participant_id="p-1",
            candidate="candidate:1 ...",
            sdp_mid="0",
            sdp_mline_index=0,
        )
        assert msg["type"] == MessageType.SERVER_WEBRTC_ICE

    def test_server_ice_candidate_negative_mline(self):
        with pytest.raises(ProtocolError):
            build_server_ice_candidate(
                room_id="room-1",
                from_participant_id="p-1",
                candidate="cand",
                sdp_mid="0",
                sdp_mline_index=-1,
            )

    def test_server_turn_credentials(self):
        msg = build_server_turn_credentials(
            urls=["turn:example.com:3478"],
            username="user",
            credential="pass",
            ttl=3600,
        )
        assert msg["type"] == MessageType.SERVER_TURN_CREDENTIALS
        assert msg["payload"]["ttl"] == 3600

    def test_server_turn_credentials_empty_urls(self):
        with pytest.raises(ProtocolError):
            build_server_turn_credentials(
                urls=[],
                username="user",
                credential="pass",
                ttl=3600,
            )

    def test_server_turn_credentials_zero_ttl(self):
        with pytest.raises(ProtocolError):
            build_server_turn_credentials(
                urls=["turn:example.com"],
                username="user",
                credential="pass",
                ttl=0,
            )

    def test_server_event_end_completed(self):
        msg = build_server_event_end(reason="completed", server_ts=9_999)
        assert msg["type"] == MessageType.SERVER_EVENT_END
        assert msg["payload"]["reason"] == "completed"

    def test_server_event_end_with_message(self):
        msg = build_server_event_end(
            reason="cancelled", server_ts=9_999, message="Event cancelled by organiser"
        )
        assert msg["payload"]["message"] == "Event cancelled by organiser"

    def test_server_event_end_invalid_reason(self):
        with pytest.raises(ProtocolError):
            build_server_event_end(reason="unknown_reason", server_ts=9_999)

    def test_server_error_minimal(self):
        msg = build_server_error(code="ERR_INTERNAL", message="Something went wrong")
        assert msg["type"] == MessageType.SERVER_ERROR
        assert "original_type" not in msg["payload"]
        assert "detail" not in msg["payload"]

    def test_server_error_full(self):
        msg = build_server_error(
            code="ERR_INVALID_MESSAGE",
            message="Missing field",
            original_type="client.hello",
            detail={"field": "participant_token"},
        )
        assert msg["payload"]["original_type"] == "client.hello"
        assert msg["payload"]["detail"]["field"] == "participant_token"


# ---------------------------------------------------------------------------
# 12. error_from_protocol_error
# ---------------------------------------------------------------------------


class TestErrorFromProtocolError:
    def test_basic(self):
        exc = ProtocolError(
            ErrorCode.ERR_INVALID_JSON,
            "bad json",
            original_type=None,
        )
        msg = error_from_protocol_error(exc)
        assert msg["type"] == MessageType.SERVER_ERROR
        assert msg["payload"]["code"] == "ERR_INVALID_JSON"
        assert msg["payload"]["message"] == "bad json"

    def test_with_original_type(self):
        exc = ProtocolError(
            ErrorCode.ERR_INVALID_MESSAGE,
            "Missing field",
            original_type="client.hello",
            detail={"field": "client_ts"},
        )
        msg = error_from_protocol_error(exc)
        assert msg["payload"]["original_type"] == "client.hello"
        assert msg["payload"]["detail"] == {"field": "client_ts"}

    def test_rejects_non_protocol_error(self):
        with pytest.raises(TypeError):
            error_from_protocol_error(ValueError("oops"))  # type: ignore[arg-type]

    def test_output_is_json_serialisable(self):
        exc = ProtocolError(
            ErrorCode.ERR_UNKNOWN_TYPE, "Unknown", original_type="foo.bar"
        )
        msg = error_from_protocol_error(exc)
        serialised = json.dumps(msg)
        parsed = json.loads(serialised)
        assert parsed["type"] == "server.error"


# ---------------------------------------------------------------------------
# 13. validate_payload direct usage
# ---------------------------------------------------------------------------


class TestValidatePayload:
    def test_valid_client_hello_payload(self):
        validate_payload(
            MessageType.CLIENT_HELLO,
            {"participant_token": "tok", "client_ts": 1_000},
        )

    def test_invalid_payload_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            validate_payload(MessageType.CLIENT_HELLO, {"client_ts": 1_000})
        assert exc_info.value.code == ErrorCode.ERR_INVALID_MESSAGE

    def test_unknown_type_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            validate_payload("nonexistent.type", {})
        assert exc_info.value.code == ErrorCode.ERR_UNKNOWN_TYPE


# ---------------------------------------------------------------------------
# 14. build_message generic helper
# ---------------------------------------------------------------------------


class TestBuildMessage:
    def test_envelope_structure(self):
        msg = build_message(
            MessageType.CLIENT_PING,
            client_ts=1_000_000,
        )
        assert set(msg.keys()) == {"type", "version", "payload"}
        assert msg["type"] == "client.ping"
        assert msg["version"] == PROTOCOL_VERSION
        assert msg["payload"]["client_ts"] == 1_000_000

    def test_invalid_payload_raises(self):
        with pytest.raises(ProtocolError):
            build_message(MessageType.CLIENT_PING)  # missing client_ts

    def test_json_serialisable(self):
        msg = build_message(MessageType.CLIENT_PING, client_ts=1_000_000)
        json.dumps(msg)  # should not raise


# ---------------------------------------------------------------------------
# 15. Round-trip: build → serialise → validate
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Build a server message, serialise it, then validate it back."""

    def test_server_hello_round_trip(self):
        msg = build_server_hello(
            participant_id="p-abc",
            server_ts=2_000_000,
            client_ts_echo=1_000_000,
            event_id="evt-1",
        )
        raw = json.dumps(msg)
        msg_type, payload = validate_message(raw)
        assert msg_type == MessageType.SERVER_HELLO
        assert payload["participant_id"] == "p-abc"

    def test_server_pairing_round_trip(self):
        msg = build_server_pairing(
            round_number=1,
            room_id="room-99",
            partner_id="p-2",
            round_start_ts=5_000,
            round_end_ts=10_000,
            partner_tags=["tech"],
        )
        raw = json.dumps(msg)
        msg_type, payload = validate_message(raw)
        assert msg_type == MessageType.SERVER_PAIRING
        assert payload["partner_tags"] == ["tech"]

    def test_server_error_round_trip(self):
        msg = build_server_error(code="ERR_INTERNAL", message="Oops")
        raw = json.dumps(msg)
        msg_type, payload = validate_message(raw)
        assert msg_type == MessageType.SERVER_ERROR
        assert payload["code"] == "ERR_INTERNAL"

    def test_server_turn_credentials_round_trip(self):
        msg = build_server_turn_credentials(
            urls=["turn:relay.example.com:3478"],
            username="user123",
            credential="cred-xyz",
            ttl=86400,
        )
        raw = json.dumps(msg)
        msg_type, payload = validate_message(raw)
        assert msg_type == MessageType.SERVER_TURN_CREDENTIALS
        assert payload["ttl"] == 86400

    def test_server_event_end_round_trip(self):
        msg = build_server_event_end(reason="completed", server_ts=99_999)
        raw = json.dumps(msg)
        msg_type, payload = validate_message(raw)
        assert msg_type == MessageType.SERVER_EVENT_END
        assert payload["reason"] == "completed"

    def test_client_webrtc_ice_round_trip(self):
        raw = _wrap(
            MessageType.CLIENT_WEBRTC_ICE,
            {
                "room_id": "room-1",
                "candidate": "candidate:1 ...",
                "sdp_mid": "0",
                "sdp_mline_index": 2,
            },
        )
        msg_type, payload = validate_message(raw)
        assert msg_type == MessageType.CLIENT_WEBRTC_ICE
        assert payload["sdp_mline_index"] == 2


# ---------------------------------------------------------------------------
# 16. Error code exhaustiveness
# ---------------------------------------------------------------------------


class TestErrorCodeExhaustiveness:
    """Every ErrorCode must have a meaningful, non-empty string value."""

    def test_all_error_codes_have_values(self):
        for code in ErrorCode:
            assert code.value, f"{code.name} has an empty value"
            assert code.value.startswith("ERR_"), (
                f"{code.value} does not start with ERR_"
            )

    def test_known_codes_present(self):
        expected = {
            "ERR_INVALID_JSON",
            "ERR_INVALID_MESSAGE",
            "ERR_UNKNOWN_TYPE",
            "ERR_VERSION_MISMATCH",
            "ERR_NOT_AUTHENTICATED",
            "ERR_ALREADY_CONNECTED",
            "ERR_INVALID_STATE",
            "ERR_WRONG_ROOM",
            "ERR_RATE_LIMITED",
            "ERR_INTERNAL",
        }
        actual = {c.value for c in ErrorCode}
        missing = expected - actual
        assert not missing, f"Missing expected error codes: {missing}"


# ---------------------------------------------------------------------------
# 17. Version negotiation edge cases
# ---------------------------------------------------------------------------


class TestVersionNegotiation:
    def test_version_0_rejected(self):
        raw = json.dumps(
            {"type": "client.ping", "version": 0, "payload": {"client_ts": 1}}
        )
        _assert_protocol_error(raw, ErrorCode.ERR_VERSION_MISMATCH)

    def test_version_2_rejected_until_supported(self):
        assert 2 not in SUPPORTED_VERSIONS
        raw = json.dumps(
            {"type": "client.ping", "version": 2, "payload": {"client_ts": 1}}
        )
        _assert_protocol_error(raw, ErrorCode.ERR_VERSION_MISMATCH)

    def test_version_1_accepted(self):
        raw = json.dumps(
            {"type": "client.ping", "version": 1, "payload": {"client_ts": 1_000}}
        )
        msg_type, _ = validate_message(raw)
        assert msg_type == MessageType.CLIENT_PING
