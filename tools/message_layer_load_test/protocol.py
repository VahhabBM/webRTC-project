"""T-13/T-14 client envelopes built through the shared protocol module."""

from __future__ import annotations

from apps.protocol.constants import MessageType
from apps.protocol.schemas import build_message


def now_ts_ms() -> int:
    from datetime import UTC, datetime

    return int(datetime.now(UTC).timestamp() * 1000)


def client_hello(*, client_ts: int) -> dict:
    return build_message(MessageType.CLIENT_HELLO, client_ts=client_ts)


def client_clock_sync(*, client_ts: int) -> dict:
    return build_message(MessageType.CLIENT_CLOCK_SYNC, client_ts=client_ts)


def client_ping(*, client_ts: int) -> dict:
    return build_message(MessageType.CLIENT_PING, client_ts=client_ts)


def client_ready(*, round_number: int) -> dict:
    return build_message(MessageType.CLIENT_READY, round_number=round_number)
