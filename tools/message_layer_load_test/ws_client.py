"""One synthetic T-14 WebSocket client. Failures stay on this instance."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter, defaultdict, deque
from typing import Any, Protocol

from apps.protocol.constants import MessageType

from .http_auth import authenticate_join_token
from .protocol import client_clock_sync, client_hello, client_ready, now_ts_ms
from .report import redact_text


class MessageSocket(Protocol):
    async def send_json(self, message: dict) -> None: ...

    async def recv_json(self) -> dict: ...

    async def close(self) -> None: ...


class SocketConnectError(RuntimeError):
    """The WebSocket transport could not be established."""


def safe_error(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}")[:500]


class WebSocketMessageSocket:
    """Adapter over the ``websockets`` client library."""

    def __init__(self, connection):
        self._connection = connection

    async def send_json(self, message: dict) -> None:
        await self._connection.send(json.dumps(message))

    async def recv_json(self) -> dict:
        raw = await self._connection.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def close(self) -> None:
        close = getattr(self._connection, "close", None)
        if close:
            await close()


async def connect_websocket(
    ws_url: str,
    session_cookie: str,
    *,
    cookie_name: str = "sessionid",
    open_timeout: float = 10.0,
) -> WebSocketMessageSocket:
    """Open ``/ws/events/`` with the T-14 session cookie. Lazy-imports websockets."""
    try:
        import websockets
    except ImportError as exc:
        raise SocketConnectError(
            "The 'websockets' package is required for live load tests."
        ) from exc

    headers = {"Cookie": f"{cookie_name}={session_cookie}"}
    kwargs: dict[str, Any] = {"open_timeout": open_timeout}
    # websockets 13+ renamed extra_headers -> additional_headers.
    try:
        connect = websockets.connect(ws_url, additional_headers=headers, **kwargs)
    except TypeError:
        connect = websockets.connect(ws_url, extra_headers=headers, **kwargs)
    try:
        connection = await connect
    except Exception as exc:
        raise SocketConnectError(safe_error(exc)) from exc
    return WebSocketMessageSocket(connection)


class SyntheticClient:
    """Authenticate, handshake, measure RTT, and collect T-24 lifecycle messages."""

    def __init__(
        self,
        index: int,
        *,
        http_base: str,
        ws_url: str,
        join_token: str,
        participant_id: str = "",
        cookie_name: str = "sessionid",
        handshake_timeout: float = 10.0,
        authenticator=authenticate_join_token,
        socket_factory=connect_websocket,
    ):
        self.index = index
        self.http_base = http_base
        self.ws_url = ws_url
        self.participant_id = participant_id
        self.cookie_name = cookie_name
        self.handshake_timeout = handshake_timeout
        self._join_token = join_token
        self._authenticator = authenticator
        self._socket_factory = socket_factory
        self._session_cookie: str | None = None
        self._socket: MessageSocket | None = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self.authenticated = False
        self.connected = False
        self.handshake_ok = False
        self.error: str | None = None
        self.stage = "created"
        self.sent_by_type: Counter[str] = Counter()
        self.received_by_type: Counter[str] = Counter()
        self.expected_by_type: Counter[str] = Counter()
        self.latency_ms: list[float] = []
        self._pending_sync: dict[int, deque[float]] = defaultdict(deque)
        self._recv_error: str | None = None

    def discard_secrets(self) -> None:
        self._join_token = ""
        self._session_cookie = None

    async def authenticate(self) -> None:
        self.stage = "authenticate"
        result = await asyncio.to_thread(
            self._authenticator,
            self.http_base,
            self._join_token,
            timeout=self.handshake_timeout,
            cookie_name=self.cookie_name,
        )
        self._session_cookie = result["session_cookie"]
        if result.get("participant_id"):
            self.participant_id = result["participant_id"]
        self.authenticated = True
        self._join_token = ""

    async def connect(self) -> None:
        self.stage = "connect"
        if not self._session_cookie:
            raise SocketConnectError("Session cookie missing; authenticate first.")
        self._socket = await self._socket_factory(
            self.ws_url,
            self._session_cookie,
            cookie_name=self.cookie_name,
            open_timeout=self.handshake_timeout,
        )
        self.connected = True
        self._recv_task = asyncio.create_task(
            self._recv_loop(), name=f"t44-recv-{self.index}"
        )

    async def handshake(self) -> None:
        self.stage = "handshake"
        self.expected_by_type[MessageType.SERVER_HELLO] += 1
        await self._send(client_hello(client_ts=now_ts_ms()))
        if not await self.wait_for(
            MessageType.SERVER_HELLO, timeout=self.handshake_timeout
        ):
            raise RuntimeError("Timed out waiting for server.hello.")
        self.handshake_ok = True

    async def run_clock_sync(self, samples: int) -> None:
        self.stage = "clock_sync"
        count = max(0, int(samples))
        self.expected_by_type[MessageType.SERVER_CLOCK_SYNC] += count
        for _ in range(count):
            client_ts = now_ts_ms()
            self._pending_sync[client_ts].append(time.perf_counter())
            await self._send(client_clock_sync(client_ts=client_ts))
        if count and not await self.wait_for(
            MessageType.SERVER_CLOCK_SYNC,
            count=count,
            timeout=self.handshake_timeout,
        ):
            # Incomplete samples are counted as loss; do not fail the client.
            return

    def expect_lifecycle(self) -> None:
        self.expected_by_type[MessageType.SERVER_PAIRING] += 2
        self.expected_by_type[MessageType.SERVER_ROUND_START] += 1
        self.expected_by_type[MessageType.SERVER_ROUND_END] += 1

    async def send_ready(self, round_number: int) -> None:
        await self._send(client_ready(round_number=round_number))

    async def wait_for(
        self, msg_type: str, *, count: int = 1, timeout: float = 10.0
    ) -> bool:
        deadline = time.monotonic() + timeout
        while self.received_by_type[msg_type] < count:
            if self._closed or self._recv_error:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))
        return True

    async def close(self) -> None:
        self._closed = True
        self.discard_secrets()
        task = self._recv_task
        self._recv_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        socket = self._socket
        self._socket = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass

    async def _send(self, message: dict) -> None:
        if self._socket is None:
            raise RuntimeError("Socket is not connected.")
        await self._socket.send_json(message)
        self.sent_by_type[str(message.get("type") or "unknown")] += 1

    async def _recv_loop(self) -> None:
        assert self._socket is not None
        try:
            while not self._closed:
                message = await self._socket.recv_json()
                self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._recv_error = safe_error(exc)
            if not self.error:
                self.error = self._recv_error

    def _handle_message(self, message: dict) -> None:
        msg_type = str(message.get("type") or "unknown")
        self.received_by_type[msg_type] += 1
        if msg_type == MessageType.SERVER_HELLO:
            payload = message.get("payload") or {}
            if payload.get("participant_id"):
                self.participant_id = str(payload["participant_id"])
        elif msg_type == MessageType.SERVER_CLOCK_SYNC:
            self._record_clock_sync(message.get("payload") or {})
        elif msg_type == MessageType.SERVER_ERROR and not self.handshake_ok:
            payload = message.get("payload") or {}
            self.error = redact_text(str(payload.get("code") or "server.error"))

    def _record_clock_sync(self, payload: dict) -> None:
        try:
            echo = int(payload.get("client_ts_echo"))
        except (TypeError, ValueError):
            return
        pending = self._pending_sync.get(echo)
        if not pending:
            return
        started = pending.popleft()
        if not pending:
            self._pending_sync.pop(echo, None)
        self.latency_ms.append((time.perf_counter() - started) * 1000.0)

    def snapshot(self) -> dict[str, Any]:
        expected = int(sum(self.expected_by_type.values()))
        matched = sum(
            min(self.received_by_type.get(key, 0), count)
            for key, count in self.expected_by_type.items()
        )
        return {
            "index": self.index,
            "participant_id": self.participant_id,
            "authenticated": self.authenticated,
            "connected": self.connected,
            "handshake_ok": self.handshake_ok,
            "stage": self.stage,
            "error": self.error,
            "sent": int(sum(self.sent_by_type.values())),
            "expected": expected,
            "received": int(sum(self.received_by_type.values())),
            "matched_expected": matched,
            "lost": max(0, expected - matched),
            "latency_sample_count": len(self.latency_ms),
        }
