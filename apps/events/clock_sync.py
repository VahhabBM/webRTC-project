"""NTP-style server clock estimation used by clients and tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import median
from time import monotonic, time


@dataclass(frozen=True)
class ClockSample:
    client_send_ms: int
    server_ms: int
    client_receive_ms: int
    rtt_ms: float
    offset_ms: float


def make_sample(
    client_send_ms: int,
    server_ms: int,
    client_receive_ms: int,
    *,
    monotonic_send: float,
    monotonic_receive: float,
) -> ClockSample:
    rtt_ms = max(0.0, (monotonic_receive - monotonic_send) * 1000)
    return ClockSample(
        client_send_ms,
        server_ms,
        client_receive_ms,
        rtt_ms,
        server_ms - (client_send_ms + client_receive_ms) / 2,
    )


@dataclass
class ClockSyncState:
    samples: list[ClockSample] = field(default_factory=list)
    offset_ms: float | None = None
    rtt_ms: float | None = None
    status: str = "unsynchronised"

    def add(self, sample: ClockSample, *, max_rtt_ms: float = 2000) -> bool:
        if sample.rtt_ms < 0 or sample.rtt_ms > max_rtt_ms:
            return False
        self.samples.append(sample)
        return True

    def finalize(self, *, minimum_samples: int = 3) -> None:
        if not self.samples:
            self.status, self.offset_ms, self.rtt_ms = "unsynchronised", None, None
            return
        self.offset_ms = float(median(s.offset_ms for s in self.samples))
        self.rtt_ms = float(median(s.rtt_ms for s in self.samples))
        self.status = (
            "synchronised" if len(self.samples) >= minimum_samples else "insufficient"
        )

    @property
    def sample_count(self) -> int:
        return len(self.samples)

    def estimated_server_time_ms(self, client_time_ms: int | None = None) -> int | None:
        if self.offset_ms is None:
            return None
        return round(
            (int(time() * 1000) if client_time_ms is None else client_time_ms)
            + self.offset_ms
        )


def monotonic_ms(clock: Callable[[], float] = monotonic) -> float:
    return clock() * 1000
