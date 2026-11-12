from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.events.auth import issue_join_token, personal_join_link
from apps.events.models import Event, Participant, RegistrationStatus
from apps.events.orchestrator import connection_channel_key
from apps.events.presence import (
    _presence_key,
    get_online_participant_ids,
    publish_presence,
    remove_presence,
    touch_presence,
)


@pytest.fixture
def presence_participants(db):
    event = Event.objects.create(
        name="Presence", start_time=timezone.now(), num_rounds=1
    )
    confirmed = Participant.objects.create(
        event=event, display_name="Confirmed", join_token_hash="!"
    )
    pending = Participant.objects.create(
        event=event,
        display_name="Pending",
        join_token_hash="!",
        registration_status=RegistrationStatus.PENDING,
    )
    return event, confirmed, pending


@pytest.mark.django_db(transaction=True)
def test_presence_requires_owned_fresh_ready_confirmed_socket(presence_participants, settings):
    event, participant, pending = presence_participants
    settings.LIVE_PRESENCE_TTL_SECONDS = 45
    cache.clear()
    cache.set(connection_channel_key(participant.pk), "channel-a", 300)
    cache.set(connection_channel_key(pending.pk), "channel-p", 300)

    assert publish_presence(participant, "channel-a")
    assert get_online_participant_ids(event) == set()
    assert touch_presence(participant, "channel-a", ready=True)
    assert get_online_participant_ids(event) == {str(participant.pk)}

    # Replacement sockets take ownership; an old socket cannot refresh or remove it.
    cache.set(connection_channel_key(participant.pk), "channel-b", 300)
    assert not touch_presence(participant, "channel-a")
    assert not remove_presence(participant, "channel-a")
    assert get_online_participant_ids(event) == set()


@pytest.mark.django_db(transaction=True)
def test_presence_lease_expires_and_remove_is_channel_scoped(presence_participants):
    event, participant, _pending = presence_participants
    cache.clear()
    cache.set(connection_channel_key(participant.pk), "channel-a", 300)
    assert publish_presence(participant, "channel-a", ready=True)
    future = timezone.now() + timedelta(seconds=46)
    assert get_online_participant_ids(event, now=future) == set()
    assert remove_presence(participant, "other-channel") is False
    assert get_online_participant_ids(event) == {str(participant.pk)}
    assert remove_presence(participant, "channel-a")
    assert get_online_participant_ids(event) == set()


@pytest.mark.django_db(transaction=True)
def test_valid_ping_restores_expired_lease_without_losing_ready(presence_participants):
    from channels.db import database_sync_to_async
    from channels.testing import WebsocketCommunicator

    from apps.protocol.constants import PROTOCOL_VERSION, MessageType
    from config.asgi import application

    event, participant, _pending = presence_participants
    cache.clear()
    token = issue_join_token(participant)
    client = Client()
    assert client.get(personal_join_link(participant, token)).status_code == 200
    cookie = client.cookies["sessionid"].value
    communicator = WebsocketCommunicator(
        application,
        "/ws/events/",
        headers=[(b"cookie", f"sessionid={cookie}".encode())],
    )

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected
        await communicator.send_json_to(
            {
                "type": MessageType.CLIENT_HELLO,
                "version": PROTOCOL_VERSION,
                "payload": {"client_ts": 1_700_000_000_000},
            }
        )
        assert (await communicator.receive_json_from())["type"] == MessageType.SERVER_HELLO
        await communicator.send_json_to(
            {
                "type": MessageType.CLIENT_READY,
                "version": PROTOCOL_VERSION,
                "payload": {"round_number": 0},
            }
        )
        await communicator.send_json_to(
            {
                "type": MessageType.CLIENT_PING,
                "version": PROTOCOL_VERSION,
                "payload": {"client_ts": 1_700_000_000_001},
            }
        )
        assert (await communicator.receive_json_from())["type"] == MessageType.SERVER_PONG
        record = cache.get(_presence_key(participant.pk))
        assert record and record["ready"] is True
        assert await database_sync_to_async(get_online_participant_ids)(event) == {
            str(participant.pk)
        }
        cache.delete(_presence_key(participant.pk))
        await communicator.send_json_to(
            {
                "type": MessageType.CLIENT_PING,
                "version": PROTOCOL_VERSION,
                "payload": {"client_ts": 1_700_000_000_002},
            }
        )
        assert (await communicator.receive_json_from())["type"] == MessageType.SERVER_PONG
        assert await database_sync_to_async(get_online_participant_ids)(event) == {
            str(participant.pk)
        }
        await communicator.disconnect()

    async_to_sync(scenario)()
