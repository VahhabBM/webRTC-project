"""T-33 5–20s network-loss reconnect — focused contract and node tests."""

from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from asgiref.sync import async_to_sync
from channels.layers import channel_layers
from django.core.cache import cache
from django.utils import timezone

from apps.events.call_room_logic import (
    RECONNECT_INITIAL_DELAY_MS,
    RECONNECT_MAX_DELAY_MS,
    RECONNECT_WINDOW_MS,
    CallRoomPhase,
    CallRoomState,
    compute_remaining_ms,
    next_reconnect_delay_ms,
    reconnect_window_exhausted,
)
from apps.events.models import Round
from apps.events.orchestrator import OrchestratorRealtime
from apps.events.scheduler import RoundScheduler
from apps.protocol.constants import MessageType
from tests.test_orchestrator_realtime import _make_event, _pair, _participant
from tests.test_websocket_auth import _connect, _cookie_for, _hello

REPO = Path(__file__).resolve().parents[1]
NEGOTIATOR_JS = (
    REPO / "apps" / "events" / "static" / "events" / "js" / "perfect_negotiator.js"
)
NEGOTIATOR_JS_COPY = REPO / "apps" / "events" / "js" / "perfect_negotiator.js"
CALL_ROOM_JS = REPO / "apps" / "events" / "static" / "events" / "js" / "call_room.js"
VIDEO_ROOM_HTML = REPO / "apps" / "events" / "templates" / "events" / "video_room.html"
NODE_TEST = REPO / "tests" / "js" / "test_transient_reconnect.mjs"


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


def test_backoff_is_bounded_exponential():
    assert next_reconnect_delay_ms(0) == 500
    assert next_reconnect_delay_ms(1) == 1000
    assert next_reconnect_delay_ms(2) == 2000
    assert next_reconnect_delay_ms(3) == 4000
    assert next_reconnect_delay_ms(8) == 4000
    assert RECONNECT_INITIAL_DELAY_MS == 500
    assert RECONNECT_MAX_DELAY_MS == 4000
    assert RECONNECT_WINDOW_MS == 45_000
    assert reconnect_window_exhausted(0, 44_999) is False
    assert reconnect_window_exhausted(0, 45_000) is True


def test_call_room_reconnect_contract_reuses_t14_and_t31():
    src = CALL_ROOM_JS.read_text(encoding="utf-8")
    assert "client.hello" in src
    assert "/ws/events/" in src
    assert "nextReconnectDelayMs" in src
    assert "RECONNECTING" in src
    assert "recoverAfterSignalingRestore" in src
    assert "navigator.mediaDevices.getUserMedia" not in src
    assert "_giveUpReconnect" in src
    assert "online" in src
    assert src.count("new WebSocket") <= 1

    negotiator = NEGOTIATOR_JS.read_text(encoding="utf-8")
    assert "recoverAfterSignalingRestore" in negotiator
    assert "_maybeRestartIce" in negotiator
    assert (
        "getUserMedia"
        not in negotiator.split("recoverAfterSignalingRestore()")[1][:400]
    )


def test_video_room_exposes_reconnecting_state():
    html = VIDEO_ROOM_HTML.read_text(encoding="utf-8")
    assert "reconnecting" in html
    assert 'id="connectionBadge"' in html
    assert 'id="timerDisplay"' in html
    assert "statusFooter" in html


def test_transient_disconnect_preserves_partner_round_and_timer():
    state = CallRoomState().on_pairing(_pairing_payload())
    state = state.on_round_start({"round_number": 1})
    disconnected = state.on_transient_disconnect()
    assert disconnected.phase == CallRoomPhase.IN_ROUND
    assert disconnected.phase != CallRoomPhase.EVENT_ENDED
    assert disconnected.round_number == 1
    assert disconnected.round_end_ts == 1_700_000_300_000
    assert disconnected.partner is not None
    assert disconnected.partner.room_id == "room-1"

    remaining = compute_remaining_ms(disconnected.round_end_ts, 0, 1_700_000_100_000)
    remaining_again = compute_remaining_ms(
        disconnected.round_end_ts, 0, 1_700_000_100_000
    )
    assert remaining == remaining_again == 200_000


def test_idle_disconnect_still_uses_disconnected_phase():
    state = CallRoomState().on_transient_disconnect()
    assert state.phase == CallRoomPhase.DISCONNECTED
    assert state.phase != CallRoomPhase.EVENT_ENDED


def test_reconnect_snapshot_is_isolated_to_one_participant(setup_round):
    event = setup_round["event"]
    p1 = setup_round["p1"]
    p3 = setup_round["p3"]
    r1 = setup_round["r1"]
    now = r1.starts_at + timedelta(seconds=1)
    orch = OrchestratorRealtime(event, now_fn=lambda: now)

    messages = orch.reconnect_snapshot_messages(p1, now=now)
    types = [msg["type"] for msg in messages]
    assert MessageType.SERVER_PAIRING in types
    assert MessageType.SERVER_ROUND_START in types
    assert MessageType.SERVER_EVENT_END not in types
    assert MessageType.SERVER_ROUND_END not in types
    pairing = next(msg for msg in messages if msg["type"] == MessageType.SERVER_PAIRING)
    assert pairing["payload"]["partner_id"] == str(setup_round["p2"].pk)
    assert pairing["payload"]["room_id"] == "room-r1-a"
    assert pairing["payload"]["round_number"] == 1
    assert pairing["payload"]["round_end_ts"] == int(r1.ends_at.timestamp() * 1000)

    other_room = orch.reconnect_snapshot_messages(p3, now=now)
    other_pairing = next(
        msg for msg in other_room if msg["type"] == MessageType.SERVER_PAIRING
    )
    assert other_pairing["payload"]["room_id"] == "room-r1-b"
    assert other_pairing["payload"]["partner_id"] == str(setup_round["p4"].pk)


def test_reconnect_snapshot_does_not_end_a_live_round(setup_round):
    event = setup_round["event"]
    r1 = setup_round["r1"]
    now = r1.starts_at + timedelta(seconds=5)
    orch = OrchestratorRealtime(event, now_fn=lambda: now)
    messages = orch.reconnect_snapshot_messages(setup_round["p1"], now=now)
    assert all(msg["type"] != MessageType.SERVER_EVENT_END for msg in messages)
    assert all(msg["type"] != MessageType.SERVER_ROUND_END for msg in messages)


def test_reconnect_after_event_completion_replays_event_end(setup_round):
    event = setup_round["event"]
    last = setup_round["r2"]
    now = last.ends_at + timedelta(seconds=1)
    orch = OrchestratorRealtime(event, now_fn=lambda: now)
    messages = orch.reconnect_snapshot_messages(setup_round["p1"], now=now)
    assert [msg["type"] for msg in messages] == [MessageType.SERVER_EVENT_END]


@pytest.mark.django_db(transaction=True)
def test_t14_reconnect_replays_snapshot_only_on_that_socket(setup_round, settings):
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
    }
    channel_layers.backends = {}
    p1 = setup_round["p1"]
    p3 = setup_round["p3"]
    cache.clear()
    now = timezone.now()
    r1 = setup_round["r1"]
    r2 = setup_round["r2"]
    r1.starts_at = now - timedelta(seconds=5)
    r1.ends_at = now + timedelta(minutes=5)
    r1.save(update_fields=["starts_at", "ends_at"])
    r2.starts_at = now + timedelta(minutes=10)
    r2.ends_at = now + timedelta(minutes=15)
    r2.save(update_fields=["starts_at", "ends_at"])
    cookie_a = _cookie_for(p1)
    cookie_c = _cookie_for(p3)

    async def scenario():
        first = _connect(cookie_a)
        other = _connect(cookie_c)
        assert (await first.connect())[0]
        assert (await other.connect())[0]
        await first.send_json_to(_hello())
        hello = await first.receive_json_from()
        assert hello["type"] == MessageType.SERVER_HELLO
        await other.send_json_to(_hello())
        assert (await other.receive_json_from())["type"] == MessageType.SERVER_HELLO
        await first.disconnect()

        second = _connect(cookie_a)
        assert (await second.connect())[0]
        await second.send_json_to(_hello())
        hello2 = await second.receive_json_from()
        assert hello2["type"] == MessageType.SERVER_HELLO
        pairing = await second.receive_json_from()
        start = await second.receive_json_from()
        assert pairing["type"] == MessageType.SERVER_PAIRING
        assert pairing["payload"]["room_id"] == "room-r1-a"
        assert pairing["payload"]["partner_id"] == str(setup_round["p2"].pk)
        assert start["type"] == MessageType.SERVER_ROUND_START
        assert start["payload"]["room_id"] == "room-r1-a"

        await other.send_json_to(
            {
                "type": "client.ping",
                "version": 1,
                "payload": {"client_ts": 1},
            }
        )
        pong = await other.receive_json_from()
        assert pong["type"] == MessageType.SERVER_PONG
        await second.disconnect()
        await other.disconnect()

    async_to_sync(scenario)()


@pytest.fixture
def setup_round(db):
    event = _make_event()
    p1 = _participant(event, "Alice")
    p2 = _participant(event, "Bob")
    p3 = _participant(event, "Cara")
    p4 = _participant(event, "Drew")
    scheduler = RoundScheduler(event)
    scheduler.persist_schedule()
    r1 = Round.objects.get(event=event, number=1)
    r2 = Round.objects.get(event=event, number=2)
    pair_1 = _pair(event, r1, p1, p2, "room-r1-a")
    pair_2 = _pair(event, r1, p3, p4, "room-r1-b")
    return {
        "event": event,
        "p1": p1,
        "p2": p2,
        "p3": p3,
        "p4": p4,
        "r1": r1,
        "r2": r2,
        "pair_1": pair_1,
        "pair_2": pair_2,
    }


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required for JS reconnect tests"
)
def test_node_transient_reconnect():
    completed = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
