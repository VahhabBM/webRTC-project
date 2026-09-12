"""T-30 call-room timer, lifecycle, and partner-info tests."""

import pytest

from apps.events.call_room_logic import (
    CallRoomPhase,
    CallRoomState,
    PartnerInfo,
    TimerVisualState,
    compute_remaining_ms,
    format_timer_display,
    parse_pairing_payload,
    timer_visual_state,
)
from apps.events.clock_sync import ClockSyncState, make_sample


def _pairing_payload(**overrides):
    base = {
        "round_number": 2,
        "room_id": "room-abc",
        "partner_id": "00000000-0000-0000-0000-000000000002",
        "is_offerer": True,
        "round_start_ts": 1_700_000_000_000,
        "round_end_ts": 1_700_000_300_000,
        "partner_display_name": "Sara",
        "partner_tags": ["ai", "music"],
    }
    base.update(overrides)
    return base


def test_compute_remaining_ms_uses_clock_offset():
    # Server is 500 ms ahead of client; round ends at t=10000 server time.
    offset_ms = 500
    client_now = 9000
    round_end_ts = 10000
    # estimated server now = 9000 + 500 = 9500; remaining = 500
    assert compute_remaining_ms(round_end_ts, offset_ms, client_now) == 500


def test_compute_remaining_ms_never_negative():
    assert compute_remaining_ms(1000, 0, 5000) == 0


def test_format_timer_display():
    assert format_timer_display(125_000) == "02:05"
    assert format_timer_display(59_999) == "00:59"
    assert format_timer_display(0) == "00:00"


def test_timer_visual_state_normal():
    assert (
        timer_visual_state(60_000, warning_threshold_seconds=30)
        == TimerVisualState.NORMAL
    )


def test_timer_visual_state_warning_from_threshold():
    assert (
        timer_visual_state(29_999, warning_threshold_seconds=30)
        == TimerVisualState.WARNING
    )


def test_timer_visual_state_warning_from_server_flag():
    assert (
        timer_visual_state(
            120_000, warning_threshold_seconds=30, server_warning_active=True
        )
        == TimerVisualState.WARNING
    )


def test_timer_visual_state_expired():
    assert (
        timer_visual_state(0, warning_threshold_seconds=30) == TimerVisualState.EXPIRED
    )


def test_parse_pairing_payload_full():
    partner = parse_pairing_payload(_pairing_payload())
    assert partner == PartnerInfo(
        partner_id="00000000-0000-0000-0000-000000000002",
        room_id="room-abc",
        round_number=2,
        round_start_ts=1_700_000_000_000,
        round_end_ts=1_700_000_300_000,
        is_offerer=True,
        display_name="Sara",
        tags=("ai", "music"),
    )


def test_parse_pairing_payload_defaults_without_optional_fields():
    partner = parse_pairing_payload(
        _pairing_payload(partner_display_name=None, partner_tags=None)
    )
    assert partner.display_name == "Partner"
    assert partner.tags == ()


def test_lifecycle_pairing_to_round_start():
    state = CallRoomState()
    state = state.on_pairing(_pairing_payload())
    assert state.phase == CallRoomPhase.PRECONNECTING
    assert state.partner is not None
    assert state.partner.display_name == "Sara"
    assert state.round_end_ts == 1_700_000_300_000

    state = state.on_round_start({"round_number": 2})
    assert state.phase == CallRoomPhase.IN_ROUND
    assert state.server_warning_active is False


def test_lifecycle_round_warning_sets_flag():
    state = CallRoomState().on_pairing(_pairing_payload())
    state = state.on_round_start({"round_number": 2})
    state = state.on_round_warning(
        {"round_number": 2, "remaining_seconds": 25, "round_end_ts": 1_700_000_300_000}
    )
    assert state.server_warning_active is True
    assert state.phase == CallRoomPhase.IN_ROUND


def test_lifecycle_round_end_clears_partner():
    state = CallRoomState().on_pairing(_pairing_payload())
    state = state.on_round_start({"round_number": 2})
    state = state.on_round_end({"round_number": 2})
    assert state.phase == CallRoomPhase.ROUND_ENDING
    assert state.partner is None


def test_lifecycle_round_end_then_next_pairing():
    state = CallRoomState().on_pairing(_pairing_payload(round_number=1))
    state = state.on_round_start({"round_number": 1})
    state = state.on_round_end({"round_number": 1})
    state = state.on_pairing(
        _pairing_payload(
            round_number=2,
            partner_display_name="Alex",
            partner_tags=["tech"],
            round_end_ts=1_700_000_600_000,
        )
    )
    assert state.phase == CallRoomPhase.PRECONNECTING
    assert state.round_number == 2
    assert state.partner is not None
    assert state.partner.display_name == "Alex"
    assert state.partner.tags == ("tech",)


def test_lifecycle_event_end():
    state = CallRoomState().on_pairing(_pairing_payload())
    state = state.on_event_end({"reason": "completed"})
    assert state.phase == CallRoomPhase.EVENT_ENDED
    assert state.event_end_reason == "completed"
    assert state.partner is None


def test_lifecycle_disconnect_preserves_round_context():
    state = CallRoomState().on_pairing(_pairing_payload())
    state = state.on_disconnect()
    assert state.phase == CallRoomPhase.DISCONNECTED
    assert state.round_number == 2


def test_timer_with_clock_sync_state_integration():
    """End-to-end: T-15 offset + T-30 remaining calculation."""
    sync = ClockSyncState()
    for _ in range(3):
        sync.add(
            make_sample(
                1_000,
                1_500,
                1_100,
                monotonic_send=0.0,
                monotonic_receive=0.1,
            )
        )
    sync.finalize()
    assert sync.offset_ms == pytest.approx(450.0)

    remaining = compute_remaining_ms(
        round_end_ts=10_000,
        offset_ms=sync.offset_ms,
        client_now_ms=9_000,
    )
    # estimated server now = 9000 + 450 = 9450; remaining = 550
    assert remaining == 550
