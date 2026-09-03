from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.sessions.models import Session
from django.test import Client
from django.utils import timezone

from apps.events.auth import issue_join_token, personal_join_link
from apps.events.models import Event, Participant
from apps.protocol.constants import (
    CLOSE_AUTHENTICATION_FAILED,
    CLOSE_INTERNAL_ERROR,
    CLOSE_MESSAGE_TOO_BIG,
    CLOSE_NORMAL,
    CLOSE_POLICY_VIOLATION,
    CLOSE_VERSION_MISMATCH,
    PROTOCOL_VERSION,
    ErrorCode,
    MessageType,
)
from config.asgi import application


@pytest.fixture
def event(db):
    return Event.objects.create(
        name="WebSocket event",
        num_rounds=1,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(),
        start_time=timezone.now(),
    )


def _participant(event, name):
    return Participant.objects.create(
        event=event, display_name=name, join_token_hash="!"
    )


def _cookie_for(participant):
    token = issue_join_token(participant)
    client = Client()
    assert client.get(personal_join_link(participant, token)).status_code == 200
    return client.cookies["sessionid"].value


def _hello(ts=1_700_000_000_000, **extra_payload):
    return {
        "type": MessageType.CLIENT_HELLO,
        "version": PROTOCOL_VERSION,
        "payload": {"client_ts": ts, **extra_payload},
    }


def _connect(cookie=None):
    headers = [(b"cookie", f"sessionid={cookie}".encode())] if cookie else []
    return WebsocketCommunicator(application, "/ws/events/", headers=headers)


@pytest.mark.django_db(transaction=True)
def test_authenticated_handshake_uses_session_identity(event):
    participant = _participant(event, "Alice")
    communicator = _connect(_cookie_for(participant))

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected
        await communicator.send_json_to(_hello(participant_id="client-controlled"))
        hello = await communicator.receive_json_from()
        assert hello["type"] == MessageType.SERVER_HELLO
        assert hello["payload"]["participant_id"] == str(participant.pk)
        await communicator.disconnect()

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_invalid_state_and_unknown_type_do_not_crash_connection(event):
    participant = _participant(event, "Alice")
    communicator = _connect(_cookie_for(participant))

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected
        await communicator.send_json_to(
            {
                "type": MessageType.CLIENT_PING,
                "version": PROTOCOL_VERSION,
                "payload": {"client_ts": 1},
            }
        )
        assert (await communicator.receive_json_from())["payload"]["code"] == (
            ErrorCode.ERR_INVALID_STATE
        )
        await communicator.send_json_to(
            {"type": "client.unknown", "version": PROTOCOL_VERSION, "payload": {}}
        )
        assert (await communicator.receive_json_from())["payload"]["code"] == (
            ErrorCode.ERR_UNKNOWN_TYPE
        )
        await communicator.send_json_to(_hello())
        assert (await communicator.receive_json_from())["type"] == (
            MessageType.SERVER_HELLO
        )
        await communicator.disconnect()

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_unauthenticated_socket_gets_protocol_error_and_4001():
    communicator = _connect()

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected
        error = await communicator.receive_json_from()
        assert error["payload"]["code"] == ErrorCode.ERR_NOT_AUTHENTICATED
        output = await communicator.receive_output()
        assert output["type"] == "websocket.close"
        assert output["code"] == CLOSE_AUTHENTICATION_FAILED

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_invalid_session_is_rejected(event):
    participant = _participant(event, "Alice")
    cookie = _cookie_for(participant)

    Session.objects.filter(session_key=cookie).delete()
    communicator = _connect(cookie)

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected
        error = await communicator.receive_json_from()
        assert error["payload"]["code"] == ErrorCode.ERR_NOT_AUTHENTICATED
        assert (await communicator.receive_output())["code"] == (
            CLOSE_AUTHENTICATION_FAILED
        )

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_disconnect_then_reconnect_gets_fresh_authenticated_state(event):
    participant = _participant(event, "Alice")
    cookie = _cookie_for(participant)

    async def scenario():
        first = _connect(cookie)
        connected, _ = await first.connect()
        assert connected
        await first.send_json_to(_hello())
        await first.receive_json_from()
        await first.disconnect()

        second = _connect(cookie)
        connected, _ = await second.connect()
        assert connected
        await second.send_json_to(_hello())
        hello = await second.receive_json_from()
        assert hello["payload"]["participant_id"] == str(participant.pk)
        await second.disconnect()

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_rate_limit_uses_configured_value(event, settings):
    settings.PROTOCOL_RATE_LIMIT_MESSAGES_PER_MINUTE = 1
    participant = _participant(event, "Alice")
    communicator = _connect(_cookie_for(participant))

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected
        await communicator.send_json_to(_hello())
        await communicator.receive_json_from()
        await communicator.send_json_to(
            {
                "type": MessageType.CLIENT_PING,
                "version": PROTOCOL_VERSION,
                "payload": {"client_ts": 1},
            }
        )
        error = await communicator.receive_json_from()
        assert error["payload"]["code"] == ErrorCode.ERR_RATE_LIMITED
        output = await communicator.receive_output()
        assert output["code"] == CLOSE_POLICY_VIOLATION

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_dead_connection_is_closed_after_configured_timeout(event, settings):
    settings.WEBSOCKET_HEARTBEAT_INTERVAL_SECONDS = 0.01
    settings.WEBSOCKET_HEARTBEAT_TIMEOUT_SECONDS = 0.02
    participant = _participant(event, "Alice")
    communicator = _connect(_cookie_for(participant))

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected
        output = await communicator.receive_output(timeout=1)
        assert output["type"] == "websocket.close"
        assert output["code"] == CLOSE_NORMAL

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_two_participants_are_isolated_and_duplicate_connections_allowed(event):
    alice = _participant(event, "Alice")
    bob = _participant(event, "Bob")
    one = _connect(_cookie_for(alice))
    two = _connect(_cookie_for(bob))
    duplicate = _connect(_cookie_for(alice))

    async def scenario():
        for communicator in (one, two, duplicate):
            connected, _ = await communicator.connect()
            assert connected
            await communicator.send_json_to(_hello())
        responses = [await c.receive_json_from() for c in (one, two, duplicate)]
        assert responses[0]["payload"]["participant_id"] == str(alice.pk)
        assert responses[1]["payload"]["participant_id"] == str(bob.pk)
        assert responses[2]["payload"]["participant_id"] == str(alice.pk)
        await one.disconnect()
        await two.send_json_to(
            {
                "type": MessageType.CLIENT_PING,
                "version": PROTOCOL_VERSION,
                "payload": {"client_ts": 1_700_000_000_001},
            }
        )
        assert (await two.receive_json_from())["type"] == MessageType.SERVER_PONG
        await two.disconnect()
        await duplicate.disconnect()

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_protocol_errors_are_canonical_and_fatal_version_mismatch(event):
    participant = _participant(event, "Alice")
    communicator = _connect(_cookie_for(participant))

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected
        await communicator.send_to(text_data="{")
        error = await communicator.receive_json_from()
        assert error["type"] == MessageType.SERVER_ERROR
        assert error["payload"]["code"] == ErrorCode.ERR_INVALID_JSON
        await communicator.send_json_to({**_hello(), "version": 999})
        error = await communicator.receive_json_from()
        assert error["payload"]["code"] == ErrorCode.ERR_VERSION_MISMATCH
        output = await communicator.receive_output()
        assert output["code"] == CLOSE_VERSION_MISMATCH

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_oversized_message_uses_1009(event, settings):
    settings.WEBSOCKET_MAX_MESSAGE_BYTES = 10
    participant = _participant(event, "Alice")
    communicator = _connect(_cookie_for(participant))

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected
        await communicator.send_to(text_data="x" * 11)
        error = await communicator.receive_json_from()
        assert error["payload"]["code"] == ErrorCode.ERR_INVALID_MESSAGE
        assert (await communicator.receive_output())["code"] == CLOSE_MESSAGE_TOO_BIG

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_internal_failure_is_redacted_and_closed(event, monkeypatch):
    participant = _participant(event, "Alice")
    communicator = _connect(_cookie_for(participant))

    def fail_hello(**kwargs):
        raise RuntimeError("database password secret-path")

    monkeypatch.setattr("apps.events.consumers.build_server_hello", fail_hello)

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected
        await communicator.send_json_to(_hello())
        error = await communicator.receive_json_from()
        assert error["payload"]["code"] == ErrorCode.ERR_INTERNAL
        assert "password" not in error["payload"]["message"]
        assert "path" not in error["payload"]["message"]
        assert (await communicator.receive_output())["code"] == CLOSE_INTERNAL_ERROR

    async_to_sync(scenario)()
