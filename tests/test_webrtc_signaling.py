import asyncio
from datetime import timedelta

import pytest
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from apps.events.models import Event, Pair, Participant, Round
from apps.protocol.constants import ErrorCode, MessageType
from apps.protocol.schemas import (
    build_client_ice_candidate,
    build_client_webrtc_answer,
    build_client_webrtc_offer,
)
from config.asgi import application
from tests.test_websocket_auth import _cookie_for, _hello


@pytest.fixture
def signaling_setup(db):
    now = timezone.now()
    event = Event.objects.create(
        name="Signaling Event",
        num_rounds=1,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(),
        start_time=now,
    )
    p_a = Participant.objects.create(
        event=event, display_name="UserA", join_token_hash="!"
    )
    p_b = Participant.objects.create(
        event=event, display_name="UserB", join_token_hash="!"
    )
    round_1 = Round.objects.create(
        event=event,
        number=1,
        starts_at=now,
        ends_at=now + timedelta(minutes=5),
    )
    pair = Pair.objects.create(
        event=event,
        round=round_1,
        participant_a=p_a,
        participant_b=p_b,
        room_id="room-test-100",
    )
    return event, p_a, p_b, pair


@pytest.mark.django_db(transaction=True)
def test_webrtc_signaling_relay_between_paired_participants(signaling_setup):
    _, p_a, p_b, pair = signaling_setup

    cookie_a = _cookie_for(p_a)
    cookie_b = _cookie_for(p_b)
    room_id = pair.room_id
    p_a_id = str(p_a.pk)
    p_b_id = str(p_b.pk)

    async def _run():
        comm_a = WebsocketCommunicator(
            application,
            "/ws/events/",
            headers=[(b"cookie", f"sessionid={cookie_a}".encode())],
        )
        comm_b = WebsocketCommunicator(
            application,
            "/ws/events/",
            headers=[(b"cookie", f"sessionid={cookie_b}".encode())],
        )

        connected_a, _ = await comm_a.connect()
        connected_b, _ = await comm_b.connect()
        assert connected_a and connected_b

        # Handshake & Hello
        await comm_a.send_json_to(_hello())
        res_hello_a = await comm_a.receive_json_from()
        assert res_hello_a["type"] == MessageType.SERVER_HELLO

        await comm_b.send_json_to(_hello())
        res_hello_b = await comm_b.receive_json_from()
        assert res_hello_b["type"] == MessageType.SERVER_HELLO

        # Send Offer (User A -> User B)
        offer_msg = build_client_webrtc_offer(room_id=room_id, sdp="v=0\r\ntest-offer")
        await comm_a.send_json_to(offer_msg)

        relayed_offer = await comm_b.receive_json_from()
        assert relayed_offer["type"] == MessageType.SERVER_WEBRTC_OFFER
        assert relayed_offer["payload"]["room_id"] == room_id
        assert relayed_offer["payload"]["from_participant_id"] == p_a_id
        assert relayed_offer["payload"]["sdp"] == "v=0\r\ntest-offer"

        # Send Answer (User B -> User A)
        answer_msg = build_client_webrtc_answer(
            room_id=room_id, sdp="v=0\r\ntest-answer"
        )
        await comm_b.send_json_to(answer_msg)

        relayed_answer = await comm_a.receive_json_from()
        assert relayed_answer["type"] == MessageType.SERVER_WEBRTC_ANSWER
        assert relayed_answer["payload"]["from_participant_id"] == p_b_id
        assert relayed_answer["payload"]["sdp"] == "v=0\r\ntest-answer"

        # Send ICE Candidate (User A -> User B)
        ice_msg = build_client_ice_candidate(
            room_id=room_id,
            candidate="candidate:1 1 UDP 1234 192.168.1.1 5000 typ host",
            sdp_mid="0",
            sdp_mline_index=0,
        )
        await comm_a.send_json_to(ice_msg)

        relayed_ice = await comm_b.receive_json_from()
        assert relayed_ice["type"] == MessageType.SERVER_WEBRTC_ICE
        assert relayed_ice["payload"]["from_participant_id"] == p_a_id
        assert relayed_ice["payload"]["candidate"] == ice_msg["payload"]["candidate"]

        await comm_a.disconnect()
        await comm_b.disconnect()

    asyncio.run(_run())


@pytest.mark.django_db(transaction=True)
def test_webrtc_signaling_wrong_room_rejected(signaling_setup):
    _, p_a, _, _ = signaling_setup
    cookie_a = _cookie_for(p_a)

    async def _run():
        comm_a = WebsocketCommunicator(
            application,
            "/ws/events/",
            headers=[(b"cookie", f"sessionid={cookie_a}".encode())],
        )
        connected, _ = await comm_a.connect()
        assert connected

        await comm_a.send_json_to(_hello())
        res_hello = await comm_a.receive_json_from()
        assert res_hello["type"] == MessageType.SERVER_HELLO

        # Send offer with unassigned/wrong room_id
        bad_msg = build_client_webrtc_offer(room_id="room-fake-999", sdp="v=0\r\nfake")
        await comm_a.send_json_to(bad_msg)

        err = await comm_a.receive_json_from()
        assert err["type"] == MessageType.SERVER_ERROR
        assert err["payload"]["code"] == ErrorCode.ERR_WRONG_ROOM

        await comm_a.disconnect()

    asyncio.run(_run())
