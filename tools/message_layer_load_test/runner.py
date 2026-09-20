"""Bounded concurrent runner for the T-44 message-layer load test."""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from asgiref.sync import sync_to_async

from apps.protocol.constants import MessageType

from . import DEFAULT_CLIENT_COUNT
from .metrics import loss_stats, merge_by_type, summarize_latency_ms
from .provision import (
    ProvisionedLoadTest,
    delete_load_test_event,
    provision_load_test_event,
    refuse_production,
    validate_client_count,
)
from .report import strip_secrets, write_result_json
from .ws_client import SyntheticClient, safe_error


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_http_base() -> str:
    return os.environ.get("MESSAGE_LAYER_LOAD_TEST_HTTP_BASE", "http://127.0.0.1:8000")


def default_ws_url(http_base: str | None = None) -> str:
    explicit = os.environ.get("MESSAGE_LAYER_LOAD_TEST_WS_URL")
    if explicit:
        return explicit
    base = (http_base or default_http_base()).rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/ws/events/"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/ws/events/"
    return base + "/ws/events/"


class OrchestratorBroadcaster:
    """Thin wrapper around existing T-24 ``OrchestratorRealtime`` methods."""

    def __init__(self, event_id: str):
        from apps.events.models import Event, Round
        from apps.events.orchestrator import OrchestratorRealtime

        self.event = Event.objects.get(pk=event_id)
        self.rounds = {
            row.number: row for row in Round.objects.filter(event=self.event)
        }
        self.orch = OrchestratorRealtime(self.event)

    async def pairing(self, round_number: int) -> dict:
        return await self.orch.abroadcast_pairing(self.rounds[round_number])

    async def round_start(self, round_number: int) -> dict:
        return await self.orch.abroadcast_round_start(self.rounds[round_number])

    async def round_end(self, round_number: int) -> dict:
        return await self.orch.abroadcast_round_end(self.rounds[round_number])


async def _bounded(sem: asyncio.Semaphore, factory: Callable):
    async with sem:
        return await factory()


async def _isolate(stage: str, client: SyntheticClient, coro) -> None:
    try:
        await coro
    except Exception as exc:
        client.error = f"{stage}: {safe_error(exc)}"


class LoadTestRunner:
    def __init__(
        self,
        *,
        clients: int = DEFAULT_CLIENT_COUNT,
        http_base: str | None = None,
        ws_url: str | None = None,
        output: str = "message-layer-load-test-result.json",
        connect_concurrency: int = 10,
        clock_sync_samples: int = 5,
        handshake_timeout: float = 10.0,
        lifecycle_timeout: float = 15.0,
        settle_seconds: float = 0.4,
        cleanup: bool = False,
        event_name: str | None = None,
        cookie_name: str = "sessionid",
        provisioner=provision_load_test_event,
        broadcaster_factory=OrchestratorBroadcaster,
        authenticator=None,
        socket_factory=None,
    ):
        self.client_count = validate_client_count(clients)
        self.http_base = (http_base or default_http_base()).rstrip("/")
        self.ws_url = ws_url or default_ws_url(self.http_base)
        self.output = output
        self.connect_concurrency = max(1, int(connect_concurrency))
        self.clock_sync_samples = max(0, int(clock_sync_samples))
        self.handshake_timeout = float(handshake_timeout)
        self.lifecycle_timeout = float(lifecycle_timeout)
        self.settle_seconds = max(0.0, float(settle_seconds))
        self.cleanup = bool(cleanup)
        self.event_name = event_name
        self.cookie_name = cookie_name
        self.provisioner = provisioner
        self.broadcaster_factory = broadcaster_factory
        self.authenticator = authenticator
        self.socket_factory = socket_factory

    async def run(self) -> dict[str, Any]:
        refuse_production()
        started_at = utc_now_iso()
        started_mono = datetime.now(UTC)
        fixture: ProvisionedLoadTest | None = None
        synthetic: list[SyntheticClient] = []
        broadcasts: list[dict[str, Any]] = []
        try:
            fixture = await sync_to_async(self.provisioner)(
                self.client_count, event_name=self.event_name
            )
            synthetic = self._make_clients(fixture)
            fixture.clear_secrets()
            await self._connect_all(synthetic)
            await self._clock_sync_all(synthetic)
            ready_clients = [c for c in synthetic if c.handshake_ok]
            for client in ready_clients:
                client.expect_lifecycle()
            broadcasts = await self._run_round_transition(
                ready_clients, fixture.event_id
            )
            return self._build_report(
                fixture=fixture,
                clients=synthetic,
                broadcasts=broadcasts,
                started_at=started_at,
                started_mono=started_mono,
            )
        finally:
            await self._close_all(synthetic)
            if fixture is not None:
                fixture.clear_secrets()
            if self.cleanup and fixture is not None:
                try:
                    await sync_to_async(delete_load_test_event)(fixture.event_id)
                except Exception:
                    pass

    def _make_clients(self, fixture: ProvisionedLoadTest) -> list[SyntheticClient]:
        clients: list[SyntheticClient] = []
        kwargs: dict[str, Any] = {}
        if self.authenticator is not None:
            kwargs["authenticator"] = self.authenticator
        if self.socket_factory is not None:
            kwargs["socket_factory"] = self.socket_factory
        for identity in fixture.clients:
            clients.append(
                SyntheticClient(
                    identity.index,
                    http_base=self.http_base,
                    ws_url=self.ws_url,
                    join_token=fixture.token_for(identity.index),
                    participant_id=identity.participant_id,
                    cookie_name=self.cookie_name,
                    handshake_timeout=self.handshake_timeout,
                    **kwargs,
                )
            )
        return clients

    async def _connect_all(self, clients: list[SyntheticClient]) -> None:
        sem = asyncio.Semaphore(self.connect_concurrency)

        async def start(client: SyntheticClient) -> None:
            async def body():
                await client.authenticate()
                await client.connect()
                await client.handshake()

            await _isolate("startup", client, body())

        await asyncio.gather(
            *[_bounded(sem, lambda c=client: start(c)) for client in clients],
            return_exceptions=True,
        )

    async def _clock_sync_all(self, clients: list[SyntheticClient]) -> None:
        async def sync_one(client: SyntheticClient) -> None:
            if not client.handshake_ok:
                return
            await _isolate(
                "clock_sync",
                client,
                client.run_clock_sync(self.clock_sync_samples),
            )

        await asyncio.gather(
            *[sync_one(client) for client in clients],
            return_exceptions=True,
        )

    async def _run_round_transition(
        self, clients: list[SyntheticClient], event_id: str
    ) -> list[dict[str, Any]]:
        broadcasts: list[dict[str, Any]] = []
        if not clients:
            return broadcasts
        broadcaster = await sync_to_async(self.broadcaster_factory)(event_id)

        async def wait_type(msg_type: str, count: int) -> None:
            await asyncio.gather(
                *[
                    client.wait_for(
                        msg_type, count=count, timeout=self.lifecycle_timeout
                    )
                    for client in clients
                ],
                return_exceptions=True,
            )

        async def send_ready(round_number: int) -> None:
            await asyncio.gather(
                *[
                    _isolate("ready", client, client.send_ready(round_number))
                    for client in clients
                ],
                return_exceptions=True,
            )

        pairing_r1 = await broadcaster.pairing(1)
        broadcasts.append(
            strip_secrets({"action": "pairing", "round": 1, **pairing_r1})
        )
        await wait_type(MessageType.SERVER_PAIRING, 1)
        await send_ready(1)
        if self.settle_seconds:
            await asyncio.sleep(self.settle_seconds)

        start = await broadcaster.round_start(1)
        broadcasts.append(strip_secrets({"action": "round_start", "round": 1, **start}))
        await wait_type(MessageType.SERVER_ROUND_START, 1)
        if self.settle_seconds:
            await asyncio.sleep(self.settle_seconds)

        end = await broadcaster.round_end(1)
        broadcasts.append(strip_secrets({"action": "round_end", "round": 1, **end}))
        await wait_type(MessageType.SERVER_ROUND_END, 1)
        if self.settle_seconds:
            await asyncio.sleep(self.settle_seconds)

        pairing_r2 = await broadcaster.pairing(2)
        broadcasts.append(
            strip_secrets({"action": "pairing", "round": 2, **pairing_r2})
        )
        await wait_type(MessageType.SERVER_PAIRING, 2)
        await send_ready(2)
        return broadcasts

    async def _close_all(self, clients: list[SyntheticClient]) -> None:
        if not clients:
            return
        await asyncio.gather(
            *[client.close() for client in clients],
            return_exceptions=True,
        )

    def _build_report(
        self,
        *,
        fixture: ProvisionedLoadTest,
        clients: list[SyntheticClient],
        broadcasts: list[dict[str, Any]],
        started_at: str,
        started_mono: datetime,
    ) -> dict[str, Any]:
        snapshots = [client.snapshot() for client in clients]
        sent = Counter()
        expected = Counter()
        received = Counter()
        latencies: list[float] = []
        for client in clients:
            sent.update(client.sent_by_type)
            expected.update(client.expected_by_type)
            received.update(client.received_by_type)
            latencies.extend(client.latency_ms)
        expected_total = int(sum(expected.values()))
        matched = sum(
            min(received.get(key, 0), count) for key, count in expected.items()
        )
        loss = loss_stats(expected=expected_total, received=matched)
        finished_at = utc_now_iso()
        duration_ms = int((datetime.now(UTC) - started_mono).total_seconds() * 1000)
        payload = {
            "task": "T-44",
            "tool": "message_layer_load_test",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "config": {
                "clients": self.client_count,
                "http_base": self.http_base,
                "ws_url": self.ws_url,
                "connect_concurrency": self.connect_concurrency,
                "clock_sync_samples": self.clock_sync_samples,
                "handshake_timeout_seconds": self.handshake_timeout,
                "lifecycle_timeout_seconds": self.lifecycle_timeout,
                "cleanup": self.cleanup,
                "event_id": fixture.event_id,
                "event_name": fixture.event_name,
                "round_numbers": list(fixture.round_numbers),
                "pair_counts": fixture.pair_counts,
            },
            "clients": {
                "attempted": len(clients),
                "authenticated": sum(1 for c in clients if c.authenticated),
                "connected": sum(1 for c in clients if c.connected),
                "handshake_ok": sum(1 for c in clients if c.handshake_ok),
                "failed": sum(1 for c in clients if c.error or not c.handshake_ok),
                "failures": [
                    {
                        "index": c.index,
                        "participant_id": c.participant_id,
                        "stage": c.stage,
                        "error": c.error,
                    }
                    for c in clients
                    if c.error or not c.handshake_ok
                ],
            },
            "latency_ms": summarize_latency_ms(latencies),
            "messages": {
                "sent": int(sum(sent.values())),
                "expected": expected_total,
                "received": int(sum(received.values())),
                "matched_expected": matched,
                "lost": loss["lost"],
                "loss_count": loss["lost"],
                "loss_rate": loss["loss_rate"],
                "by_type": merge_by_type(expected, received),
                "sent_by_type": dict(sent),
                "received_by_type": dict(received),
            },
            "lifecycle": {
                "sequence": [
                    "server.pairing(round=1)",
                    "client.ready(round=1)",
                    "server.round_start(round=1)",
                    "server.round_end(round=1)",
                    "server.pairing(round=2)",
                    "client.ready(round=2)",
                ],
                "broadcasts": broadcasts,
            },
            "client_snapshots": snapshots,
        }
        write_result_json(self.output, payload)
        return strip_secrets(payload)


async def run_load_test(**options: Any) -> dict[str, Any]:
    runner = LoadTestRunner(**options)
    return await runner.run()
