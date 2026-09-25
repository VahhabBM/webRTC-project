"""Latency and loss aggregation for the T-44 load-test tool."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear-interpolated percentile. ``p`` is in 0..100."""
    if not values:
        return None
    if p <= 0:
        return float(min(values))
    if p >= 100:
        return float(max(values))
    ordered = sorted(float(v) for v in values)
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    weight = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * weight


def summarize_latency_ms(samples: Sequence[float]) -> dict[str, Any]:
    if not samples:
        return {
            "sample_count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "p50": None,
            "p95": None,
            "p99": None,
        }
    values = [float(v) for v in samples]
    return {
        "sample_count": len(values),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "mean": round(sum(values) / len(values), 3),
        "p50": round(percentile(values, 50) or 0.0, 3),
        "p95": round(percentile(values, 95) or 0.0, 3),
        "p99": round(percentile(values, 99) or 0.0, 3),
    }


def add_counters(left: Mapping[str, int], right: Mapping[str, int]) -> dict[str, int]:
    total: Counter[str] = Counter(left)
    total.update(right)
    return dict(total)


def loss_stats(*, expected: int, received: int) -> dict[str, Any]:
    expected_n = max(0, int(expected))
    received_n = max(0, int(received))
    lost = max(0, expected_n - received_n)
    rate = (lost / expected_n) if expected_n else 0.0
    return {
        "expected": expected_n,
        "received": min(received_n, expected_n),
        "received_total": received_n,
        "lost": lost,
        "loss_rate": round(rate, 6),
    }


def merge_by_type(
    expected_by_type: Mapping[str, int],
    received_by_type: Mapping[str, int],
) -> dict[str, dict[str, Any]]:
    keys = sorted(set(expected_by_type) | set(received_by_type))
    return {
        key: loss_stats(
            expected=int(expected_by_type.get(key, 0)),
            received=int(received_by_type.get(key, 0)),
        )
        for key in keys
    }


def total_sent(sent_by_type: Iterable[Mapping[str, int]] | Mapping[str, int]) -> int:
    if isinstance(sent_by_type, Mapping):
        return int(sum(sent_by_type.values()))
    return int(sum(sum(item.values()) for item in sent_by_type))
