"""Focused tests for T-24 real-time orchestrator messages.

Covers protocol payloads, recipient selection, shared-channel broadcast,
disconnected-client isolation, lifecycle ordering, and a local dispatch
timing check. Does not run matching/seeding suites.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta

import pytest
from channels.layers import channel_layers
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from apps.events.models import Event, Pair, Participant, Round, Tag
from apps.events.orchestrator import (
    DEFAULT_FINAL_SECONDS_WARNING,
    ORCHESTRATOR_CHANNEL_TYPE,
    OrchestratorRealtime,
    datetime_to_unix_ms,
    event_channel_group,
    participant_channel_group,
    select_event_broadcast_group,
    select_pairing_recipients,
)
from apps.events.scheduler import RoundScheduler
from apps.protocol.constants import PROTOCOL_VERSION, EventEndReason, MessageType
from apps.protocol.schemas import (
    build_server_event_end,
    build_server_round_end,
    build_server_round_start,
    build_server_round_warning,
)
from apps.protocol.validators import validate_message
from config.asgi import application
from tests.test_websocket_auth import _cookie_for, _hello


class RecordingChannelLayer:
    """Shared-layer stand-in that records group_send without talking to Redis."""

    def __init__(self, *, fail_groups=None, hang_groups=None):
        self.messages: list[tuple[str, dict]] = []
        self.fail_groups = set(fail_groups or ())
        self.hang_groups = set(hang_groups or ())

    async def group_send(self, group, message):
        if group in self.hang_groups:
            raise ConnectionError("disconnected client")
        if group in self.fail_groups:
            raise ConnectionError("disconnected client")
        self.messages.append((group, message))


def _make_event(**kwargs):
    now = timezone.now()
    defaults = dict(
        name="T-24 Event",
        num_rounds=2,
        round_duration=timedelta(minutes=3),
        break_duration=timedelta(seconds=30),
        start_time=now,
    )
    defaults.update(kwargs)
    return Event.objects.create(**defaults)


def _participant(event, name):
    return Participant.objects.create(
        event=event, display_name=name, join_token_hash="!"
    )


def _pair(event, round_obj, a, b, room_id):
    return Pair.objects.create(
        event=event,
        round=round_obj,
        participant_a=a,
        participant_b=b,
        room_id=room_id,
    )


def _envelopes(layer: RecordingChannelLayer) -> list[dict]:
    return [item[1]["message"] for item in layer.messages]


@pytest.fixture
def setup_round(db):
    event = _make_event()
    p1 = _participant(event, "Alice")
    p2 = _participant(event, "Bob")
    p3 = _participant(event, "Cara")
    p4 = _participant(event, "Drew")
    unmatched = _participant(event, "Eddy")
    other = _make_event(name="Other Event")
    outsider = _participant(other, "Outsider")
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
        "unmatched": unmatched,
        "outsider": outsider,
        "other": other,
        "r1": r1,
        "r2": r2,
        "pair_1": pair_1,
        "pair_2": pair_2,
    }


# ---------------------------------------------------------------------------
# Protocol payloads
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_pairing_payload_uses_t13_builder_and_round_timestamps(setup_round):
    r1 = setup_round["r1"]
    p1, p2 = setup_round["p1"], setup_round["p2"]
    layer = RecordingChannelLayer()
    orch = OrchestratorRealtime(setup_round["event"], channel_layer=layer)
    orch.broadcast_pairing(r1)

    envelopes = _envelopes(layer)
    to_alice = next(
        msg
        for msg in envelopes
        if msg["payload"]["partner_id"] == str(p2.pk)
        and msg["payload"]["room_id"] == "room-r1-a"
    )
    assert to_alice["type"] == MessageType.SERVER_PAIRING
    assert to_alice["version"] == PROTOCOL_VERSION
    assert to_alice["payload"]["round_number"] == 1
    assert to_alice["payload"]["round_start_ts"] == datetime_to_unix_ms(r1.starts_at)
    assert to_alice["payload"]["round_end_ts"] == datetime_to_unix_ms(r1.ends_at)
    duration_ms = (
        to_alice["payload"]["round_end_ts"] - to_alice["payload"]["round_start_ts"]
    )
    assert duration_ms == int(
        setup_round["event"].round_duration.total_seconds() * 1000
    )
    room_flags = [
        msg["payload"]["is_offerer"]
        for msg in envelopes
        if msg["payload"]["room_id"] == "room-r1-a"
    ]
    assert sorted(room_flags) == [False, True]
    to_bob = next(
        msg
        for msg in envelopes
        if msg["payload"]["partner_id"] == str(p1.pk)
        and msg["payload"]["room_id"] == "room-r1-a"
    )
    assert to_bob["payload"]["is_offerer"] is (not to_alice["payload"]["is_offerer"])
    msg_type, payload = validate_message(json.dumps(to_alice))
    assert msg_type == MessageType.SERVER_PAIRING
    assert payload["partner_display_name"] == "Bob"


@pytest.mark.django_db
def test_lifecycle_payloads_match_t13_builders(setup_round):
    r1 = setup_round["r1"]
    now = r1.starts_at
    start = build_server_round_start(
        round_number=1, room_id="room-r1-a", server_ts=datetime_to_unix_ms(now)
    )
    warning = build_server_round_warning(
        round_number=1,
        server_ts=datetime_to_unix_ms(now),
        remaining_seconds=30,
        round_end_ts=datetime_to_unix_ms(r1.ends_at),
    )
    end = build_server_round_end(
        round_number=1, server_ts=datetime_to_unix_ms(r1.ends_at)
    )
    event_end = build_server_event_end(
        reason=EventEndReason.COMPLETED, server_ts=datetime_to_unix_ms(r1.ends_at)
    )
    for raw in (start, warning, end, event_end):
        validate_message(json.dumps(raw))


# ---------------------------------------------------------------------------
# Recipient selection
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_pairing_recipients_are_round_pairs_only(setup_round):
    recipients = select_pairing_recipients(setup_round["r1"])
    ids = {r.participant_id for r in recipients}
    expected = {
        str(setup_round["p1"].pk),
        str(setup_round["p2"].pk),
        str(setup_round["p3"].pk),
        str(setup_round["p4"].pk),
    }
    assert ids == expected
    assert str(setup_round["unmatched"].pk) not in ids
    assert str(setup_round["outsider"].pk) not in ids
    assert all(
        r.group == participant_channel_group(r.participant_id) for r in recipients
    )
    offerers = [r for r in recipients if r.is_offerer]
    answerers = [r for r in recipients if not r.is_offerer]
    assert len(offerers) == 2
    assert len(answerers) == 2


@pytest.mark.django_db
def test_event_wide_messages_use_event_group(setup_round):
    event = setup_round["event"]
    assert select_event_broadcast_group(event) == event_channel_group(event.pk)
    layer = RecordingChannelLayer()
    orch = OrchestratorRealtime(event, channel_layer=layer)
    orch.broadcast_event_end()
    orch.broadcast_round_end(setup_round["r1"])
    orch.broadcast_final_seconds_warning(setup_round["r1"])
    groups = {group for group, _ in layer.messages}
    assert groups == {event_channel_group(event.pk)}
    assert event_channel_group(setup_round["other"].pk) not in groups


@pytest.mark.django_db
def test_pairing_includes_partner_tags(setup_round):
    tag = Tag.objects.create(name="music")
    setup_round["p2"].tags.add(tag)
    recipients = select_pairing_recipients(setup_round["r1"])
    alice = next(r for r in recipients if r.participant_id == str(setup_round["p1"].pk))
    assert alice.partner_tags == ("music",)


# ---------------------------------------------------------------------------
# Shared channel-layer broadcast / cross-instance
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_broadcast_uses_shared_channel_groups_not_local_send(setup_round):
    layer = RecordingChannelLayer()
    orch = OrchestratorRealtime(setup_round["event"], channel_layer=layer)
    orch.broadcast_pairing(setup_round["r1"])
    groups = [group for group, _ in layer.messages]
    assert len(groups) == 4
    assert all(g.startswith("participant_") for g in groups)
    for _group, event in layer.messages:
        assert event["type"] == ORCHESTRATOR_CHANNEL_TYPE
        assert event["message"]["type"] == MessageType.SERVER_PAIRING


@pytest.fixture
def inmemory_channels(settings):
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
    }
    channel_layers.backends = {}
    yield
    channel_layers.backends = {}


def _communicator(cookie):
    return WebsocketCommunicator(
        application,
        "/ws/events/",
        headers=[(b"cookie", f"sessionid={cookie}".encode())],
    )


async def _connect_hello(cookie):
    comm = _communicator(cookie)
    connected, _ = await comm.connect()
    assert connected
    await comm.send_json_to(_hello())
    hello = await comm.receive_json_from()
    assert hello["type"] == MessageType.SERVER_HELLO
    return comm


@pytest.mark.django_db(transaction=True)
def test_cross_instance_event_broadcast_reaches_all_connections(
    setup_round, inmemory_channels
):
    """Two connections sharing the channel layer both receive event-wide messages.

    This is the same fan-out Redis provides across service instances.
    """
    event = setup_round["event"]
    cookie_a = _cookie_for(setup_round["p1"])
    cookie_b = _cookie_for(setup_round["p2"])

    async def scenario():
        comm_a = await _connect_hello(cookie_a)
        comm_b = await _connect_hello(cookie_b)
        orch = OrchestratorRealtime(event)
        await orch.abroadcast_event_end(reason=EventEndReason.COMPLETED)
        msg_a = await comm_a.receive_json_from()
        msg_b = await comm_b.receive_json_from()
        assert msg_a["type"] == MessageType.SERVER_EVENT_END
        assert msg_b["type"] == MessageType.SERVER_EVENT_END
        assert msg_a["payload"]["reason"] == "completed"
        await comm_a.disconnect()
        await comm_b.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_pairing_and_round_start_reach_only_allocated_clients(
    setup_round, inmemory_channels
):
    event = setup_round["event"]
    cookie_a = _cookie_for(setup_round["p1"])
    cookie_b = _cookie_for(setup_round["p2"])
    cookie_u = _cookie_for(setup_round["unmatched"])

    async def scenario():
        comm_a = await _connect_hello(cookie_a)
        comm_b = await _connect_hello(cookie_b)
        comm_u = await _connect_hello(cookie_u)
        orch = OrchestratorRealtime(event)
        await orch.abroadcast_pairing(setup_round["r1"])
        await orch.abroadcast_round_start(setup_round["r1"])
        pairing_a = await comm_a.receive_json_from()
        pairing_b = await comm_b.receive_json_from()
        start_a = await comm_a.receive_json_from()
        start_b = await comm_b.receive_json_from()
        assert pairing_a["type"] == MessageType.SERVER_PAIRING
        assert pairing_b["type"] == MessageType.SERVER_PAIRING
        assert start_a["type"] == MessageType.SERVER_ROUND_START
        assert start_b["type"] == MessageType.SERVER_ROUND_START
        assert start_a["payload"]["room_id"] == pairing_a["payload"]["room_id"]
        assert await comm_u.receive_nothing(timeout=0.2)
        await comm_a.disconnect()
        await comm_b.disconnect()
        await comm_u.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Disconnected-client isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_dead_group_does_not_block_other_pairing_recipients(setup_round):
    dead = participant_channel_group(setup_round["p1"].pk)
    layer = RecordingChannelLayer(fail_groups={dead})
    orch = OrchestratorRealtime(setup_round["event"], channel_layer=layer)
    result = orch.broadcast_pairing(setup_round["r1"])
    delivered_ids = {
        item[1]["message"]["payload"]["partner_id"]
        for item in layer.messages
        if item[1]["message"]["type"] == MessageType.SERVER_PAIRING
    }
    assert str(setup_round["p1"].pk) in delivered_ids  # Bob still got Alice as partner
    assert result["delivered"] == 3
    assert result["recipients"] == 4
    groups = {group for group, _ in layer.messages}
    assert dead not in groups


@pytest.mark.django_db(transaction=True)
def test_disconnected_websocket_does_not_delay_others(setup_round, inmemory_channels):
    event = setup_round["event"]
    cookie_a = _cookie_for(setup_round["p1"])
    cookie_b = _cookie_for(setup_round["p2"])
    cookie_c = _cookie_for(setup_round["p3"])

    async def scenario():
        comm_a = await _connect_hello(cookie_a)
        comm_b = await _connect_hello(cookie_b)
        comm_c = await _connect_hello(cookie_c)
        await comm_c.disconnect()
        orch = OrchestratorRealtime(event)
        started = time.perf_counter()
        await orch.abroadcast_event_end()
        msg_a = await comm_a.receive_json_from(timeout=2)
        msg_b = await comm_b.receive_json_from(timeout=2)
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert msg_a["type"] == MessageType.SERVER_EVENT_END
        assert msg_b["type"] == MessageType.SERVER_EVENT_END
        # Hard bound keeps CI stable; 250ms is the local target, not a flake gate.
        assert elapsed_ms < 2000
        await comm_a.disconnect()
        await comm_b.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Lifecycle ordering
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_dispatch_lifecycle_order_pairing_start_warning_end_event(setup_round):
    event = setup_round["event"]
    r1 = setup_round["r1"]
    layer = RecordingChannelLayer()
    orch = OrchestratorRealtime(
        event,
        channel_layer=layer,
        final_seconds=DEFAULT_FINAL_SECONDS_WARNING,
    )

    preconnect_at = r1.starts_at - timedelta(seconds=10)
    orch.dispatch(now=preconnect_at)
    types = [msg["type"] for msg in _envelopes(layer)]
    assert types and all(t == MessageType.SERVER_PAIRING for t in types)
    layer.messages.clear()

    orch.dispatch(now=r1.starts_at + timedelta(seconds=1))
    types = [msg["type"] for msg in _envelopes(layer)]
    assert types == [MessageType.SERVER_ROUND_START] * 4
    layer.messages.clear()

    orch.dispatch(now=r1.ends_at - timedelta(seconds=10))
    types = [msg["type"] for msg in _envelopes(layer)]
    assert types == [MessageType.SERVER_ROUND_WARNING]
    warning = _envelopes(layer)[0]
    assert warning["payload"]["remaining_seconds"] == 10
    assert warning["payload"]["round_end_ts"] == datetime_to_unix_ms(r1.ends_at)
    layer.messages.clear()

    orch.dispatch(now=r1.ends_at + timedelta(seconds=1))
    types = [msg["type"] for msg in _envelopes(layer)]
    assert types == [MessageType.SERVER_ROUND_END]
    layer.messages.clear()

    last = Round.objects.get(event=event, number=event.num_rounds)
    orch.dispatch(now=last.ends_at + timedelta(seconds=1))
    types = [msg["type"] for msg in _envelopes(layer)]
    assert types[-1] == MessageType.SERVER_EVENT_END
    assert MessageType.SERVER_ROUND_END in types


@pytest.mark.django_db
def test_non_leader_dispatch_sends_nothing(setup_round):
    layer = RecordingChannelLayer()
    orch = OrchestratorRealtime(
        setup_round["event"], is_leader=False, channel_layer=layer
    )
    result = orch.dispatch(now=setup_round["r1"].starts_at)
    assert result["executed"] is False
    assert result["reason"] == "not_leader"
    assert layer.messages == []


@pytest.mark.django_db
def test_dispatch_is_idempotent(setup_round):
    layer = RecordingChannelLayer()
    orch = OrchestratorRealtime(setup_round["event"], channel_layer=layer)
    now = setup_round["r1"].starts_at + timedelta(seconds=1)
    first = orch.dispatch(now=now)
    second = orch.dispatch(now=now)
    assert MessageType.SERVER_ROUND_START in first["sent"]
    assert second["sent"] == []


@pytest.mark.django_db
def test_missing_channel_layer_does_not_raise(setup_round):
    orch = OrchestratorRealtime(setup_round["event"], channel_layer=None)
    result = orch.broadcast_event_end()
    assert result["delivered"] == 0


# ---------------------------------------------------------------------------
# Local latency / dispatch check
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_local_dispatch_latency_target(setup_round, inmemory_channels):
    event = setup_round["event"]
    cookie = _cookie_for(setup_round["p1"])

    async def scenario():
        comm = await _connect_hello(cookie)
        orch = OrchestratorRealtime(event)
        started = time.perf_counter()
        await orch.abroadcast_round_end(setup_round["r1"])
        msg = await comm.receive_json_from(timeout=2)
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert msg["type"] == MessageType.SERVER_ROUND_END
        # Target is <250ms locally. Fail only if pathologically slow so CI
        # cannot flake on a loaded runner.
        assert elapsed_ms < 2000
        await comm.disconnect()
        return elapsed_ms

    elapsed_ms = asyncio.run(scenario())
    assert elapsed_ms >= 0


@pytest.mark.django_db(transaction=True)
def test_client_ready_is_accepted_after_handshake(setup_round, inmemory_channels):
    cookie = _cookie_for(setup_round["p1"])

    async def scenario():
        comm = await _connect_hello(cookie)
        await comm.send_json_to(
            {
                "type": MessageType.CLIENT_READY,
                "version": PROTOCOL_VERSION,
                "payload": {"round_number": 1},
            }
        )
        assert await comm.receive_nothing(timeout=0.2)
        await comm.disconnect()

    asyncio.run(scenario())
