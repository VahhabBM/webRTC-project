"""Focused tests for the T-44 message-layer load-test tool.

Uses in-memory fakes. Does not open 50+ sockets, hit staging, or seed 900 users.
"""

from __future__ import annotations

import asyncio
import json
from http.cookiejar import Cookie, CookieJar
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.protocol.constants import PROTOCOL_VERSION, MessageType
from apps.protocol.schemas import (
    build_server_clock_sync,
    build_server_hello,
    build_server_pairing,
    build_server_round_end,
    build_server_round_start,
)
from apps.protocol.validators import validate_message
from tools.message_layer_load_test import DEFAULT_CLIENT_COUNT
from tools.message_layer_load_test.cli import build_parser, options_from_namespace
from tools.message_layer_load_test.http_auth import (
    JoinAuthError,
    authenticate_join_token,
    join_url,
    session_cookie_from_jar,
)
from tools.message_layer_load_test.metrics import (
    loss_stats,
    percentile,
    summarize_latency_ms,
)
from tools.message_layer_load_test.protocol import (
    client_clock_sync,
    client_hello,
    client_ready,
)
from tools.message_layer_load_test.provision import (
    ProvisionedLoadTest,
    ProvisionError,
    SyntheticIdentity,
    refuse_production,
    rotated_pairs,
    validate_client_count,
)
from tools.message_layer_load_test.report import strip_secrets, write_result_json
from tools.message_layer_load_test.runner import LoadTestRunner, default_ws_url
from tools.message_layer_load_test.ws_client import SyntheticClient


def test_percentile_and_latency_summary():
    samples = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert percentile([], 50) is None
    assert percentile(samples, 0) == 1.0
    assert percentile(samples, 100) == 100.0
    assert percentile(samples, 50) == 3.0
    summary = summarize_latency_ms(samples)
    assert summary["sample_count"] == 5
    assert summary["min"] == 1.0
    assert summary["max"] == 100.0
    assert summary["p50"] == 3.0


def test_loss_stats_and_empty_latency():
    stats = loss_stats(expected=10, received=7)
    assert stats["lost"] == 3
    assert stats["loss_rate"] == 0.3
    assert loss_stats(expected=0, received=0)["loss_rate"] == 0.0
    empty = summarize_latency_ms([])
    assert empty["sample_count"] == 0
    assert empty["p95"] is None


def test_report_strips_tokens_cookies_and_join_paths(tmp_path):
    payload = {
        "join_token": "p1_abcdefghijklmnopqrstuvwxyz0123456789abcd",
        "nested": {
            "url": "https://staging.example/join/p1_abcdefghijklmnopqrstuvwxyz0123456789abcd/",
            "note": "sessionid=super-secret-session",
            "ok": "server.hello",
        },
        "cookie": "should-not-leak",
    }
    path = write_result_json(tmp_path / "result.json", payload)
    text = path.read_text(encoding="utf-8")
    assert "p1_" not in text
    assert "super-secret-session" not in text
    assert "should-not-leak" not in text
    data = json.loads(text)
    assert data["cookie"] == "[REDACTED]"
    assert data["nested"]["ok"] == "server.hello"
    assert "[REDACTED]" in data["nested"]["url"]


def test_strip_secrets_redacts_error_strings():
    cleaned = strip_secrets(
        {"error": "Join failed token=p1_abcdefghijklmnopqrstuvwxyz0123456789abcd"}
    )
    assert "p1_" not in cleaned["error"]


def test_cli_defaults_and_ws_url_derivation():
    parser = build_parser()
    ns = parser.parse_args([])
    options = options_from_namespace(ns)
    assert options["clients"] == DEFAULT_CLIENT_COUNT
    assert options["connect_concurrency"] == 10
    assert options["clock_sync_samples"] == 5
    assert options["output"] == "message-layer-load-test-result.json"
    assert (
        default_ws_url("https://staging.example") == "wss://staging.example/ws/events/"
    )
    assert default_ws_url("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/ws/events/"


def test_client_envelopes_reuse_protocol_validators():
    hello = client_hello(client_ts=1_700_000_000_000)
    ping = client_clock_sync(client_ts=1_700_000_000_001)
    ready = client_ready(round_number=1)
    for message in (hello, ping, ready):
        msg_type, payload = validate_message(json.dumps(message))
        assert message["version"] == PROTOCOL_VERSION
        assert msg_type.value == message["type"]
        assert payload == message["payload"]


def test_validate_client_count_bounds():
    assert validate_client_count(50) == 50
    with pytest.raises(ProvisionError, match="even"):
        validate_client_count(5)
    with pytest.raises(ProvisionError, match="at least"):
        validate_client_count(2)
    with pytest.raises(ProvisionError, match="900"):
        validate_client_count(900)


def test_rotated_pairs_switch_partners_without_repeats():
    people = [SimpleNamespace(pk=uuid4()) for _ in range(6)]
    round_one = {frozenset((a.pk, b.pk)) for a, b in rotated_pairs(people, 0)}
    round_two = {frozenset((a.pk, b.pk)) for a, b in rotated_pairs(people, 1)}
    assert len(round_one) == 3
    assert len(round_two) == 3
    assert round_one.isdisjoint(round_two)
    for left, right in [*rotated_pairs(people, 0), *rotated_pairs(people, 1)]:
        assert left.pk < right.pk


def test_refuse_production_settings(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.production")
    with pytest.raises(ProvisionError, match="production"):
        refuse_production()


def test_join_url_and_session_cookie_parsing():
    assert join_url("https://staging.example/", "p1_abc") == (
        "https://staging.example/join/p1_abc/"
    )
    jar = CookieJar()
    jar.set_cookie(
        Cookie(
            version=0,
            name="sessionid",
            value="cookie-value",
            port=None,
            port_specified=False,
            domain="127.0.0.1",
            domain_specified=False,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
    )
    assert session_cookie_from_jar(jar) == "cookie-value"


def test_authenticate_join_token_uses_existing_join_contract():
    class FakeResponse:
        status = 200

        def read(self):
            return json.dumps(
                {
                    "authenticated": True,
                    "participant": {
                        "id": "pid-1",
                        "event_id": "evt-1",
                        "display_name": "Ada",
                    },
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener_factory(jar: CookieJar):
        class Opener:
            def open(self, request, timeout=None):
                assert "/join/p1_tok/" in request.full_url
                jar.set_cookie(
                    Cookie(
                        version=0,
                        name="sessionid",
                        value="sess-1",
                        port=None,
                        port_specified=False,
                        domain="127.0.0.1",
                        domain_specified=False,
                        domain_initial_dot=False,
                        path="/",
                        path_specified=True,
                        secure=False,
                        expires=None,
                        discard=True,
                        comment=None,
                        comment_url=None,
                        rest={},
                        rfc2109=False,
                    )
                )
                return FakeResponse()

        return Opener()

    result = authenticate_join_token(
        "http://127.0.0.1:8000",
        "p1_tok",
        opener_factory=opener_factory,
    )
    assert result["session_cookie"] == "sess-1"
    assert result["participant_id"] == "pid-1"
    assert "p1_tok" not in json.dumps(result)


def test_authenticate_join_token_rejects_unauthenticated_response():
    class FakeResponse:
        status = 200

        def read(self):
            return json.dumps({"authenticated": False}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener_factory(jar: CookieJar):
        del jar

        class Opener:
            def open(self, request, timeout=None):
                return FakeResponse()

        return Opener()

    with pytest.raises(JoinAuthError, match="not authenticated"):
        authenticate_join_token(
            "http://127.0.0.1:8000",
            "p1_tok",
            opener_factory=opener_factory,
        )


class FakeSocket:
    def __init__(self, registry: list[FakeSocket]):
        self.inbox: asyncio.Queue[dict] = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False
        registry.append(self)

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)
        msg_type = message.get("type")
        payload = message.get("payload") or {}
        if msg_type == MessageType.CLIENT_HELLO:
            await self.inbox.put(
                build_server_hello(
                    participant_id="00000000-0000-0000-0000-000000000001",
                    server_ts=2,
                    client_ts_echo=int(payload.get("client_ts") or 1),
                    event_id="00000000-0000-0000-0000-0000000000aa",
                )
            )
        elif msg_type == MessageType.CLIENT_CLOCK_SYNC:
            await asyncio.sleep(0.001)
            await self.inbox.put(
                build_server_clock_sync(
                    client_ts_echo=int(payload["client_ts"]),
                    server_ts=3,
                )
            )

    async def recv_json(self) -> dict:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True


class FakeBroadcaster:
    def __init__(self, event_id: str, registry: list[FakeSocket]):
        self.event_id = event_id
        self.registry = registry

    async def _fanout(self, message: dict) -> dict:
        for socket in self.registry:
            await socket.inbox.put(message)
        return {"delivered": len(self.registry), "type": message["type"]}

    async def pairing(self, round_number: int) -> dict:
        return await self._fanout(
            build_server_pairing(
                round_number=round_number,
                room_id=f"room-{round_number}",
                partner_id="00000000-0000-0000-0000-000000000002",
                is_offerer=True,
                round_start_ts=1,
                round_end_ts=2,
            )
        )

    async def round_start(self, round_number: int) -> dict:
        return await self._fanout(
            build_server_round_start(
                round_number=round_number,
                room_id=f"room-{round_number}",
                server_ts=4,
            )
        )

    async def round_end(self, round_number: int) -> dict:
        return await self._fanout(
            build_server_round_end(round_number=round_number, server_ts=5)
        )


def _fake_fixture(count: int, *, bad_first: bool = False) -> ProvisionedLoadTest:
    clients = [
        SyntheticIdentity(
            index=index,
            participant_id=str(uuid4()),
            display_name=f"C{index}",
        )
        for index in range(count)
    ]
    tokens = [
        ("bad" if bad_first and index == 0 else f"ok-{index}") for index in range(count)
    ]
    return ProvisionedLoadTest(
        event_id=str(uuid4()),
        event_name="T-44 unit fixture",
        round_numbers=(1, 2),
        clients=clients,
        pair_counts={1: count // 2, 2: count // 2},
        _join_tokens=tokens,
    )


def test_runner_isolates_client_failure_and_closes_sockets(tmp_path):
    registry: list[FakeSocket] = []
    fixture = _fake_fixture(4, bad_first=True)

    def provisioner(count, event_name=None):
        del count, event_name
        return fixture

    def authenticator(http_base, token, **kwargs):
        del http_base, kwargs
        if token == "bad":
            raise JoinAuthError("join denied")
        return {
            "session_cookie": f"session-{token}",
            "participant_id": str(uuid4()),
            "event_id": fixture.event_id,
            "display_name": "ok",
        }

    async def socket_factory(url, cookie, **kwargs):
        del url, cookie, kwargs
        return FakeSocket(registry)

    output = tmp_path / "t44.json"
    runner = LoadTestRunner(
        clients=4,
        http_base="http://127.0.0.1:8000",
        ws_url="ws://127.0.0.1:8000/ws/events/",
        output=str(output),
        connect_concurrency=2,
        clock_sync_samples=2,
        handshake_timeout=2.0,
        lifecycle_timeout=2.0,
        settle_seconds=0.0,
        provisioner=provisioner,
        broadcaster_factory=lambda event_id: FakeBroadcaster(event_id, registry),
        authenticator=authenticator,
        socket_factory=socket_factory,
    )
    result = asyncio.run(runner.run())

    assert output.is_file()
    assert result["clients"]["attempted"] == 4
    assert result["clients"]["handshake_ok"] == 3
    assert result["clients"]["failed"] == 1
    assert result["messages"]["sent"] > 0
    assert result["messages"]["expected"] > 0
    assert result["messages"]["lost"] == 0
    assert result["latency_ms"]["sample_count"] == 6
    assert result["lifecycle"]["broadcasts"]
    assert all(socket.closed for socket in registry)
    assert not fixture.token_for(1)
    text = output.read_text(encoding="utf-8")
    assert "p1_" not in text
    assert "session-ok" not in text
    assert "join denied" in text


def test_one_client_error_does_not_cancel_siblings():
    client_a = SyntheticClient(
        0,
        http_base="http://127.0.0.1:8000",
        ws_url="ws://127.0.0.1:8000/ws/events/",
        join_token="a",
    )
    client_b = SyntheticClient(
        1,
        http_base="http://127.0.0.1:8000",
        ws_url="ws://127.0.0.1:8000/ws/events/",
        join_token="b",
    )

    async def boom():
        raise RuntimeError("client a exploded")

    async def ok():
        client_b.handshake_ok = True

    async def scenario():
        results = await asyncio.gather(boom(), ok(), return_exceptions=True)
        assert isinstance(results[0], RuntimeError)
        assert results[1] is None
        assert client_b.handshake_ok is True
        await client_a.close()
        await client_b.close()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_provision_creates_two_rounds_with_distinct_pairs():
    from apps.events.models import Pair
    from tools.message_layer_load_test.provision import provision_load_test_event

    fixture = provision_load_test_event(4, event_name="T-44 unit event")
    try:
        assert fixture.round_numbers == (1, 2)
        assert len(fixture.clients) == 4
        assert all(token.startswith("p1_") for token in fixture._join_tokens)
        r1 = {
            frozenset((row.participant_a_id, row.participant_b_id))
            for row in Pair.objects.filter(event_id=fixture.event_id, round__number=1)
        }
        r2 = {
            frozenset((row.participant_a_id, row.participant_b_id))
            for row in Pair.objects.filter(event_id=fixture.event_id, round__number=2)
        }
        assert len(r1) == 2
        assert len(r2) == 2
        assert r1.isdisjoint(r2)
    finally:
        fixture.clear_secrets()


def test_management_command_rejects_odd_client_count():
    with pytest.raises(CommandError):
        call_command("message_layer_load_test", clients=3)
