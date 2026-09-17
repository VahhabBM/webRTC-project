"""T-31 partner switching without re-requesting camera/microphone."""

from __future__ import annotations

import subprocess
from pathlib import Path

from apps.events.call_room_logic import CallRoomPhase, CallRoomState

REPO = Path(__file__).resolve().parents[1]
NEGOTIATOR_JS = (
    REPO / "apps" / "events" / "static" / "events" / "js" / "perfect_negotiator.js"
)
NEGOTIATOR_JS_COPY = REPO / "apps" / "events" / "js" / "perfect_negotiator.js"
CALL_ROOM_JS = REPO / "apps" / "events" / "static" / "events" / "js" / "call_room.js"
VIDEO_ROOM_HTML = REPO / "apps" / "events" / "templates" / "events" / "video_room.html"
NODE_TEST = REPO / "tests" / "js" / "test_partner_switch.mjs"


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


def test_getusermedia_is_confined_to_shared_acquire():
    src = NEGOTIATOR_JS.read_text(encoding="utf-8")
    assert src.count("navigator.mediaDevices.getUserMedia") == 1
    acquire_fn = src.split("export async function acquireSharedLocalMedia")[1]
    acquire_fn = acquire_fn.split("export class PerfectNegotiator")[0]
    assert "navigator.mediaDevices.getUserMedia" in acquire_fn
    for method in ("switchPartner", "prepareRound", "endRound", "preconnect"):
        body = src.split(f"async {method}")[1].split("async ")[0]
        assert "navigator.mediaDevices.getUserMedia" not in body


def test_call_room_never_calls_getusermedia_directly():
    src = CALL_ROOM_JS.read_text(encoding="utf-8")
    assert "navigator.mediaDevices.getUserMedia" not in src
    assert "getUserMedia(" not in src
    assert "acquireSharedLocalMedia" in src
    assert "prepareRound" in src
    assert "_onPartnerSwitchFailure" in src
    assert "leave()" in src


def test_negotiator_copies_stay_in_sync():
    assert NEGOTIATOR_JS.read_text(encoding="utf-8") == NEGOTIATOR_JS_COPY.read_text(
        encoding="utf-8"
    )


def test_video_room_has_user_facing_switch_error_banner():
    html = VIDEO_ROOM_HTML.read_text(encoding="utf-8")
    assert 'id="mediaErrorBanner"' in html
    assert "errorBanner" in html


def test_python_lifecycle_round_change_does_not_end_event():
    state = CallRoomState().on_pairing(_pairing_payload())
    state = state.on_round_start({"round_number": 1})
    state = state.on_round_end({"round_number": 1})
    state = state.on_pairing(
        _pairing_payload(
            round_number=2,
            room_id="room-2",
            partner_id="00000000-0000-0000-0000-000000000003",
            partner_display_name="Alex",
        )
    )
    assert state.phase == CallRoomPhase.PRECONNECTING
    assert state.phase != CallRoomPhase.EVENT_ENDED
    assert state.partner is not None
    assert state.partner.display_name == "Alex"


def test_node_partner_switch_lifecycle():
    completed = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
