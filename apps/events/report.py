"""Matching quality report: pure computation, no ORM dependency.

build_report() takes a GeneratedSchedule and the list of ScheduleViolations
returned by validate_schedule() and computes all T-21 numeric metrics.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from apps.events.matching import GeneratedSchedule, ScheduleViolation


@dataclass
class RoundReport:
    """Per-round quality metrics."""

    number: int
    pair_count: int
    avg_score: float
    p10_score: float
    zero_tag_pairs: int
    unmatched_pid: str | None


@dataclass
class MatchingReport:
    """Aggregate quality report for a generated schedule."""

    rounds: list[RoundReport] = field(default_factory=list)
    total_pairs: int = 0
    total_zero_tag_pairs: int = 0
    # Maps unmatched pid -> number of rounds that participant received a bye.
    bye_distribution: dict[str, int] = field(default_factory=dict)
    violation_count: int = 0
    violations: list[ScheduleViolation] = field(default_factory=list)

    @property
    def can_lock(self) -> bool:
        """True only when there are zero constraint violations."""
        return self.violation_count == 0


def _percentile10(scores: list[float]) -> float:
    """10th percentile of *scores* using statistics.quantiles (decile method).

    Uses ``method='inclusive'`` so the result is always within
    [min(scores), max(scores)], even for small datasets.
    Returns 0.0 for an empty list; returns the single value when len == 1.
    """
    if not scores:
        return 0.0
    if len(scores) == 1:
        return scores[0]
    # statistics.quantiles(data, n=10, method='inclusive') yields 9 cut-points.
    # Index 0 is the 10th percentile (D1).  'inclusive' keeps the result
    # within [min, max] and avoids extrapolation for small sample sizes.
    return statistics.quantiles(scores, n=10, method="inclusive")[0]


def build_report(
    schedule: GeneratedSchedule,
    violations: list[ScheduleViolation],
) -> MatchingReport:
    """Compute quality metrics from a GeneratedSchedule.

    Parameters
    ----------
    schedule:
        The schedule produced by generate_schedule().
    violations:
        The list returned by validate_schedule() for the same schedule.
        Pass an empty list when the schedule is clean.

    Returns
    -------
    MatchingReport
        Fully populated report; can_lock is True iff len(violations) == 0.
    """
    round_reports: list[RoundReport] = []
    total_pairs = 0
    total_zero_tag = 0
    bye_distribution: dict[str, int] = {}

    for rnd in schedule.rounds:
        scores = [sp.score for sp in rnd.pairs]
        zero_tag = sum(1 for sp in rnd.pairs if sp.score == 0.0)
        avg = sum(scores) / len(scores) if scores else 0.0
        p10 = _percentile10(scores)

        round_reports.append(
            RoundReport(
                number=rnd.number,
                pair_count=len(rnd.pairs),
                avg_score=avg,
                p10_score=p10,
                zero_tag_pairs=zero_tag,
                unmatched_pid=rnd.unmatched_pid,
            )
        )
        total_pairs += len(rnd.pairs)
        total_zero_tag += zero_tag

        if rnd.unmatched_pid:
            bye_distribution[rnd.unmatched_pid] = (
                bye_distribution.get(rnd.unmatched_pid, 0) + 1
            )

    return MatchingReport(
        rounds=round_reports,
        total_pairs=total_pairs,
        total_zero_tag_pairs=total_zero_tag,
        bye_distribution=bye_distribution,
        violation_count=len(violations),
        violations=violations,
    )
