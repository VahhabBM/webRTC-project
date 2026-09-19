"""T-34 long absence + personal-link re-entry, without mid-round replacement."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.layers import channel_layers
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.events.auth import issue_join_token, personal_join_link
from apps.events.call_room_logic import (
    PARTNER_ABSENCE_GRACE_MS,
    PARTNER_GONE_FOOTER,
    CallRoomPhase,
    CallRoomState,
    PartnerPresence,
    compute_remaining_ms,
    partner_left_banner,
    partner_presence_from_protocol,
    should_announce_partner_gone,
)
from apps.events.models import Pair, Round
from apps.events.orchestrator import (
    OrchestratorRealtime,
    current_pair_for_participant,
    partner_on_pair,
)
from apps.events.scheduler import RoundScheduler
from apps.protocol.constants import ErrorCode, MessageType, PartnerState
from tests.test_orchestrator_realtime import (
    RecordingChannelLayer,
    _make_event,
    _pair,
    _participant,
)
from tests.test_websocket_auth import _connect, _cookie_for, _hello

REPO = Path(__file__).resolve().parents[1]
CALL_ROOM_JS = REPO / "apps" / "events" / "static" / "events" / "js" / "call_room.js"
VIDEO_ROOM_HTML = REPO / "apps" / "events" / "templates" / "events" / "video_room.html"
NODE_TEST = REPO / "tests" / "js" / "test_long_absence_reentry.mjs"


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


def _activate_round(setup_round, *, remaining_minutes=5):
    now = timezone.now()
    r1 = setup_round["r1"]
    r2 = setup_round["r2"]
    r1.starts_at = now - timedelta(seconds=5)
    r1.ends_at = now + timedelta(minutes=remaining_minutes)
    r1.save(update_fields=["starts_at", "ends_at"])
    r2.starts_at = now + timedelta(minutes=10)
    r2.ends_at = now + timedelta(minutes=15)
    r2.save(update_fields=["starts_at", "ends_at"])
    return now


@pytest.fixture
def inmemory_channels(settings):
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
    }
    channel_layers.backends = {}
    yield
    channel_layers.backends = {}


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


def test_partner_presence_maps_protocol_disconnected_to_gone():
    assert partner_presence_from_protocol("connected") == PartnerPresence.CONNECTED
    assert (
        partner_presence_from_protocol("reconnecting") == PartnerPresence.RECONNECTING
    )
    assert partner_presence_from_protocol("disconnected") == PartnerPresence.GONE
    assert "left" in partner_left_banner("Sara").lower()
    assert "error" not in partner_left_banner("Sara").lower()
    assert "socket" not in PARTNER_GONE_FOOTER.lower()
    assert PARTNER_ABSENCE_GRACE_MS == 25_000


def test_stale_or_replaced_sockets_do_not_mark_partner_gone():
    assert (
        should_announce_partner_gone(
            owns_connection=True,
            session_replaced=False,
            disconnected_at_ms=0,
            now_ms=25_000,
        )
        is True
    )
    assert (
        should_announce_partner_gone(
            owns_connection=True,
            session_replaced=False,
            disconnected_at_ms=0,
            now_ms=24_999,
        )
        is False
    )
    assert (
        should_announce_partner_gone(
            owns_connection=False,
            session_replaced=False,
            disconnected_at_ms=0,
            now_ms=60_000,
        )
        is False
    )
    assert (
        should_announce_partner_gone(
            owns_connection=True,
            session_replaced=True,
            disconnected_at_ms=0,
            now_ms=60_000,
        )
        is False
    )


def test_partner_gone_keeps_round_partner_room_and_timer():
    state = CallRoomState().on_pairing(_pairing_payload())
    state = state.on_round_start({"round_number": 1})
    gone = state.on_partner_state(
        {"partner_id": "00000000-0000-0000-0000-000000000002", "state": "disconnected"}
    )
    assert gone.phase == CallRoomPhase.IN_ROUND
    assert gone.partner_presence == PartnerPresence.GONE
    assert gone.partner is not None
    assert gone.partner.room_id == "room-1"
    assert gone.partner.partner_id == "00000000-0000-0000-0000-000000000002"
    assert gone.round_number == 1
    assert gone.round_end_ts == 1_700_000_300_000
    remaining = compute_remaining_ms(gone.round_end_ts, 0, 1_700_000_100_000)
    remaining_again = compute_remaining_ms(gone.round_end_ts, 0, 1_700_000_100_000)
    assert remaining == remaining_again == 200_000

    back = gone.on_partner_state(
        {"partner_id": "00000000-0000-0000-0000-000000000002", "state": "connected"}
    )
    assert back.phase == CallRoomPhase.IN_ROUND
    assert back.partner_presence == PartnerPresence.CONNECTED
    assert back.partner.room_id == "room-1"
    assert back.round_end_ts == gone.round_end_ts


def test_call_room_js_and_html_expose_partner_left_copy():
    src = CALL_ROOM_JS.read_text(encoding="utf-8")
    assert "server.partner_state" in src
    assert "PARTNER LEFT" in src
    assert "no replacement will be assigned" in src
    assert "You joined from another window" in src
    html = VIDEO_ROOM_HTML.read_text(encoding="utf-8")
    assert "partnerGoneBanner" in html
    assert "partner-gone" in html


@pytest.mark.django_db
def test_absence_does_not_create_a_replacement_pair(setup_round):
    event = setup_round["event"]
    p1 = setup_round["p1"]
    pair_1 = setup_round["pair_1"]
    now = _activate_round(setup_round)
    before = list(
        Pair.objects.filter(event=event).values_list(
            "id", "room_id", "participant_a_id", "participant_b_id", "round_id"
        )
    )
    current = current_pair_for_participant(event, p1, now=now)
    assert current is not None
    assert current.pk == pair_1.pk
    assert partner_on_pair(current, p1).pk == setup_round["p2"].pk

    orch = OrchestratorRealtime(
        event, channel_layer=RecordingChannelLayer(), now_fn=lambda: now
    )
    orch.dispatch(now=now)
    after = list(
        Pair.objects.filter(event=event).values_list(
            "id", "room_id", "participant_a_id", "participant_b_id", "round_id"
        )
    )
    assert after == before
    pair_1.refresh_from_db()
    assert pair_1.room_id == "room-r1-a"
    assert {pair_1.participant_a_id, pair_1.participant_b_id} == {
        p1.pk,
        setup_round["p2"].pk,
    }


@pytest.mark.django_db
def test_rejoin_link_replaces_previous_session(setup_round):
    p1 = setup_round["p1"]
    token = issue_join_token(p1)
    same_browser = Client()
    assert same_browser.get(personal_join_link(p1, token)).status_code == 200
    rotated_key = same_browser.session.session_key
    assert same_browser.get(personal_join_link(p1, token)).status_code == 200
    assert same_browser.session.session_key != rotated_key
    assert not Session.objects.filter(session_key=rotated_key).exists()

    first = Client()
    assert first.get(personal_join_link(p1, token)).status_code == 200
    second = Client()
    assert second.get(personal_join_link(p1, token)).status_code == 200
    assert second.session.session_key != first.session.session_key
    assert second.get("/participant/me/").json()["participant"]["id"] == str(p1.pk)
    assert first.get("/participant/me/").status_code == 401


@pytest.mark.django_db(transaction=True)
def test_long_absence_marks_partner_gone_without_reassignment(
    setup_round, inmemory_channels, settings
):
    settings.PARTNER_ABSENCE_GRACE_SECONDS = 0.05
    cache.clear()
    _activate_round(setup_round)
    event = setup_round["event"]
    p1 = setup_round["p1"]
    p2 = setup_round["p2"]
    p3 = setup_round["p3"]
    pair_before = Pair.objects.filter(event=event).count()
    cookie_a = _cookie_for(p1)
    cookie_b = _cookie_for(p2)
    cookie_c = _cookie_for(p3)

    async def scenario():
        left = _connect(cookie_a)
        staying = _connect(cookie_b)
        other_room = _connect(cookie_c)
        assert (await left.connect())[0]
        assert (await staying.connect())[0]
        assert (await other_room.connect())[0]
        await staying.send_json_to(_hello())
        assert (await staying.receive_json_from())["type"] == MessageType.SERVER_HELLO
        await other_room.send_json_to(_hello())
        assert (await other_room.receive_json_from())[
            "type"
        ] == MessageType.SERVER_HELLO
        await left.send_json_to(_hello())
        assert (await left.receive_json_from())["type"] == MessageType.SERVER_HELLO
        connected = await staying.receive_json_from()
        assert connected["type"] == MessageType.SERVER_PARTNER_STATE
        assert connected["payload"]["state"] == PartnerState.CONNECTED
        assert connected["payload"]["partner_id"] == str(p1.pk)

        await left.disconnect()
        await asyncio.sleep(0.2)
        gone = await staying.receive_json_from()
        assert gone["type"] == MessageType.SERVER_PARTNER_STATE
        assert gone["payload"]["state"] == PartnerState.DISCONNECTED
        assert gone["payload"]["partner_id"] == str(p1.pk)
        assert gone["payload"]["state"] != "error"

        await other_room.send_json_to(
            {"type": "client.ping", "version": 1, "payload": {"client_ts": 1}}
        )
        pong = await other_room.receive_json_from()
        assert pong["type"] == MessageType.SERVER_PONG

        def _rejoin():
            token = issue_join_token(p1)
            fresh = Client()
            assert fresh.get(personal_join_link(p1, token)).status_code == 200
            return fresh.cookies["sessionid"].value

        new_cookie = await database_sync_to_async(_rejoin)()
        assert new_cookie != cookie_a

        stale = _connect(cookie_a)
        assert (await stale.connect())[0]
        error = await stale.receive_json_from()
        assert error["payload"]["code"] == ErrorCode.ERR_NOT_AUTHENTICATED
        await stale.disconnect()

        returning = _connect(new_cookie)
        assert (await returning.connect())[0]
        await returning.send_json_to(_hello())
        hello = await returning.receive_json_from()
        assert hello["type"] == MessageType.SERVER_HELLO
        pairing = await returning.receive_json_from()
        start = await returning.receive_json_from()
        assert pairing["type"] == MessageType.SERVER_PAIRING
        assert pairing["payload"]["room_id"] == "room-r1-a"
        assert pairing["payload"]["partner_id"] == str(p2.pk)
        assert pairing["payload"]["round_number"] == 1
        assert start["type"] == MessageType.SERVER_ROUND_START
        assert start["payload"]["room_id"] == "room-r1-a"

        back = await staying.receive_json_from()
        assert back["type"] == MessageType.SERVER_PARTNER_STATE
        assert back["payload"]["state"] == PartnerState.CONNECTED
        assert back["payload"]["partner_id"] == str(p1.pk)

        await returning.disconnect()
        await staying.disconnect()
        await other_room.disconnect()

    async_to_sync(scenario)()
    assert Pair.objects.filter(event=event).count() == pair_before
    pair = Pair.objects.get(pk=setup_round["pair_1"].pk)
    assert pair.room_id == "room-r1-a"
    assert {pair.participant_a_id, pair.participant_b_id} == {p1.pk, p2.pk}


@pytest.mark.django_db(transaction=True)
def test_newer_session_kicks_stale_socket(setup_round, inmemory_channels, settings):
    settings.PARTNER_ABSENCE_GRACE_SECONDS = 0.05
    cache.clear()
    _activate_round(setup_round)
    p1 = setup_round["p1"]
    cookie = _cookie_for(p1)

    async def scenario():
        first = _connect(cookie)
        assert (await first.connect())[0]
        await first.send_json_to(_hello())
        assert (await first.receive_json_from())["type"] == MessageType.SERVER_HELLO

        second = _connect(cookie)
        assert (await second.connect())[0]
        await second.send_json_to(_hello())
        assert (await second.receive_json_from())["type"] == MessageType.SERVER_HELLO
        pairing = await second.receive_json_from()
        start = await second.receive_json_from()
        assert pairing["type"] == MessageType.SERVER_PAIRING
        assert pairing["payload"]["room_id"] == "room-r1-a"
        assert start["type"] == MessageType.SERVER_ROUND_START

        replaced = await first.receive_json_from()
        assert replaced["type"] == MessageType.SERVER_ERROR
        assert replaced["payload"]["code"] == ErrorCode.ERR_ALREADY_CONNECTED
        assert "another window" in replaced["payload"]["message"].lower()
        close = await first.receive_output()
        assert close["type"] == "websocket.close"

        await second.send_json_to(
            {"type": "client.ping", "version": 1, "payload": {"client_ts": 2}}
        )
        assert (await second.receive_json_from())["type"] == MessageType.SERVER_PONG
        await second.disconnect()

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_brief_drop_within_grace_does_not_mark_partner_gone(
    setup_round, inmemory_channels, settings
):
    settings.PARTNER_ABSENCE_GRACE_SECONDS = 0.4
    cache.clear()
    _activate_round(setup_round)
    p1 = setup_round["p1"]
    p2 = setup_round["p2"]
    cookie_a = _cookie_for(p1)
    cookie_b = _cookie_for(p2)

    async def scenario():
        left = _connect(cookie_a)
        staying = _connect(cookie_b)
        assert (await left.connect())[0]
        assert (await staying.connect())[0]
        await staying.send_json_to(_hello())
        assert (await staying.receive_json_from())["type"] == MessageType.SERVER_HELLO
        await left.send_json_to(_hello())
        assert (await left.receive_json_from())["type"] == MessageType.SERVER_HELLO
        assert (await staying.receive_json_from())[
            "type"
        ] == MessageType.SERVER_PARTNER_STATE

        await left.disconnect()
        await asyncio.sleep(0.05)
        returning = _connect(cookie_a)
        assert (await returning.connect())[0]
        await returning.send_json_to(_hello())
        assert (await returning.receive_json_from())["type"] == MessageType.SERVER_HELLO
        pairing = await returning.receive_json_from()
        start = await returning.receive_json_from()
        assert pairing["type"] == MessageType.SERVER_PAIRING
        assert pairing["payload"]["room_id"] == "room-r1-a"
        assert start["type"] == MessageType.SERVER_ROUND_START

        back = await staying.receive_json_from()
        assert back["type"] == MessageType.SERVER_PARTNER_STATE
        assert back["payload"]["state"] == PartnerState.CONNECTED
        await returning.disconnect()
        await staying.disconnect()

    async_to_sync(scenario)()


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required for JS absence tests"
)
def test_node_long_absence_reentry():
    completed = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
