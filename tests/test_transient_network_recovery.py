"""T-32 transient network drop recovery — focused contract and node tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apps.events.call_room_logic import (
    CallRoomPhase,
    CallRoomState,
    compute_remaining_ms,
)

REPO = Path(__file__).resolve().parents[1]
NEGOTIATOR_JS = (
    REPO / "apps" / "events" / "static" / "events" / "js" / "perfect_negotiator.js"
)
NEGOTIATOR_JS_COPY = REPO / "apps" / "events" / "js" / "perfect_negotiator.js"
CALL_ROOM_JS = REPO / "apps" / "events" / "static" / "events" / "js" / "call_room.js"
VIDEO_ROOM_HTML = REPO / "apps" / "events" / "templates" / "events" / "video_room.html"
NODE_TEST = REPO / "tests" / "js" / "test_transient_network_recovery.mjs"


def _pairing_payload(**overrides):
    base = {
        "round_number": 1,
        "room_id": "room-1",
        "partner_id": "00000000-0000-0000-0000-000000000002",
        "is_offerer": True,
        "round_start_ts": 1_700_000_000_000,
        "round_end_ts": 1_700_000_300_000,
        "partner_display_name": "Sara",
        "partner_tags": ["ai"],
    }
    base.update(overrides)
    return base


def test_negotiator_copies_stay_in_sync():
    assert NEGOTIATOR_JS.read_text(encoding="utf-8") == NEGOTIATOR_JS_COPY.read_text(
        encoding="utf-8"
    )


def test_recovery_paths_do_not_call_getusermedia():
    src = NEGOTIATOR_JS.read_text(encoding="utf-8")
    assert src.count("navigator.mediaDevices.getUserMedia") == 1
    assert "restartIce" in src
    assert "iceRestart: true" in src
    assert "TRANSIENT_ICE_GRACE_MS" in src
    assert "_schedulePermanentFailure" in src
    assert "_maybeRestartIce" in src
    restart_fn = src.split("  _maybeRestartIce()")[1].split(
        "  async _legacyIceRestartOffer()"
    )[0]
    legacy_fn = src.split("  async _legacyIceRestartOffer()")[1].split(
        "  _schedulePermanentFailure()"
    )[0]
    assert "getUserMedia" not in restart_fn
    assert "getUserMedia" not in legacy_fn


def test_call_room_keeps_round_on_transient_signaling_drop():
    src = CALL_ROOM_JS.read_text(encoding="utf-8")
    assert "_onSignalingClosed" in src
    assert "_applyConnectionQuality" in src
    assert "QUALITY DROP" in src
    assert "navigator.mediaDevices.getUserMedia" not in src
    assert "ensureLocalMedia" in src


def test_video_room_exposes_quality_indicator():
    html = VIDEO_ROOM_HTML.read_text(encoding="utf-8")
    assert 'id="connectionBadge"' in html
    assert 'id="qualityBanner"' in html
    assert "qualityBanner" in html
    assert ".badge.degraded" in html


def test_timer_is_independent_of_connection_quality():
    remaining = compute_remaining_ms(10_000, 250, 8_000)
    remaining_again = compute_remaining_ms(10_000, 250, 8_000)
    assert remaining == remaining_again == 1750


def test_round_lifecycle_is_unchanged_by_client_quality_drop():
    state = CallRoomState().on_pairing(_pairing_payload())
    state = state.on_round_start({"round_number": 1})
    assert state.phase == CallRoomPhase.IN_ROUND
    assert state.round_end_ts == 1_700_000_300_000
    disconnected = state.on_disconnect()
    assert disconnected.round_end_ts == 1_700_000_300_000
    assert disconnected.round_number == 1
    assert disconnected.partner is not None


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required for JS recovery tests"
)
def test_node_transient_network_recovery():
    completed = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
