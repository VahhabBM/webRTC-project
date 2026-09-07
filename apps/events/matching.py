from __future__ import annotations

import math
import uuid as _uuid_mod
from dataclasses import dataclass

from apps.events.scoring import (
    ParticipantProfile,
    ScoringWeights,
    compute_match_score,
)


class MatchingError(Exception):
    """Base class for all matching engine errors."""


class MatchingImpossibleError(MatchingError):
    """Raised when the hard constraints make schedule generation impossible."""


@dataclass(frozen=True)
class MatchParticipant:
    pid: str
    profile: ParticipantProfile


@dataclass(frozen=True)
class MatchInput:
    participants: tuple[MatchParticipant, ...]
    num_rounds: int
    weights: ScoringWeights


@dataclass(frozen=True)
class SchedulePair:
    pid_a: str
    pid_b: str
    score: float


@dataclass(frozen=True)
class ScheduleRound:
    number: int
    pairs: tuple[SchedulePair, ...]
    unmatched_pid: str | None


@dataclass(frozen=True)
class GeneratedSchedule:
    rounds: tuple[ScheduleRound, ...]


def _canon(x: str, y: str) -> tuple[str, str]:
    """Return (x, y) or (y, x) such that uuid.UUID(result[0]) < uuid.UUID(result[1])."""

    return (x, y) if _uuid_mod.UUID(x) < _uuid_mod.UUID(y) else (y, x)


def generate_schedule(match_input: MatchInput) -> GeneratedSchedule:
    """Generate an R-round no-repeat pairing schedule entirely in memory."""

    pid_list: list[str] = [p.pid for p in match_input.participants]
    profiles: dict[str, ParticipantProfile] = {
        p.pid: p.profile for p in match_input.participants
    }
    weights = match_input.weights

    score_cache: dict[tuple[str, str], float] = {}
    for i in range(len(pid_list)):
        for j in range(i + 1, len(pid_list)):
            key = _canon(pid_list[i], pid_list[j])
            score_cache[key] = compute_match_score(
                profiles[pid_list[i]],
                profiles[pid_list[j]],
                weights,
            )

    def _score(x: str, y: str) -> float:
        return score_cache[_canon(x, y)]

    N = len(pid_list)
    R = match_input.num_rounds

    if N < 2:
        raise MatchingImpossibleError(
            f"Cannot generate a schedule with fewer than 2 participants (got {N})."
        )

    if N <= R:
        raise MatchingImpossibleError(
            f"Participant count {N} is too small for {R} non-repeating rounds. "
            f"Each participant can meet at most {N - 1} unique partners, but {R} "
            f"rounds require {R} distinct partners for at least one participant. "
            f"Need N >= R+1 = {R + 1}."
        )

    forbidden: dict[str, set[str]] = {pid: set() for pid in pid_list}
    unmatched_count: dict[str, int] = {pid: 0 for pid in pid_list}

    rounds_list: list[ScheduleRound] = []

    for round_num in range(1, R + 1):
        if N % 2 == 1:
            eligible = [pid for pid in pid_list if unmatched_count[pid] == 0]
            if not eligible:
                raise MatchingImpossibleError(
                    "Cannot satisfy 'no participant unmatched more than once' in "
                    f"round {round_num}: all participants have already been unmatched."
                )

            avail_for_bye: dict[str, int] = {
                pid: len(set(pid_list) - {pid} - forbidden[pid]) for pid in eligible
            }
            bye_pid: str = max(eligible, key=lambda p: (avail_for_bye[p], p))

            unmatched_count[bye_pid] += 1
            working_set: set[str] = set(pid_list) - {bye_pid}
        else:
            bye_pid = None
            working_set = set(pid_list)

        avail_count: dict[str, int] = {
            pid: len((working_set - {pid}) - forbidden[pid]) for pid in working_set
        }

        queue: list[str] = sorted(
            working_set,
            key=lambda pid: (avail_count[pid], pid),
        )

        round_used: set[str] = set()
        round_pairs: list[SchedulePair] = []

        for pid in queue:
            if pid in round_used:
                continue

            candidates: list[str] = [
                q
                for q in working_set
                if q not in round_used and q != pid and q not in forbidden[pid]
            ]

            if not candidates:
                raise MatchingImpossibleError(
                    f"Participant {pid} has no valid partner in round {round_num}. "
                    f"forbidden={len(forbidden[pid])}, working_set={len(working_set)}, "
                    f"round_used={len(round_used)}."
                )

            best: str = sorted(candidates, key=lambda q: (-_score(pid, q), q))[0]

            a, b = _canon(pid, best)
            round_pairs.append(SchedulePair(pid_a=a, pid_b=b, score=_score(a, b)))
            round_used.add(pid)
            round_used.add(best)

            forbidden[pid].add(best)
            forbidden[best].add(pid)

        completed_round = ScheduleRound(
            number=round_num,
            pairs=tuple(sorted(round_pairs, key=lambda sp: (sp.pid_a, sp.pid_b))),
            unmatched_pid=bye_pid,
        )
        rounds_list.append(completed_round)

    MAX_PASSES = 10

    all_pairs: set[tuple[str, str]] = {
        _canon(sp.pid_a, sp.pid_b) for rnd in rounds_list for sp in rnd.pairs
    }

    def _in_other_rounds(
        key: tuple[str, str], current_round_set: set[tuple[str, str]]
    ) -> bool:
        return key in all_pairs and key not in current_round_set

    improved_rounds: list[ScheduleRound] = []

    for rnd in rounds_list:
        current_pairs: list[SchedulePair] = list(rnd.pairs)
        current_round_set: set[tuple[str, str]] = {
            _canon(sp.pid_a, sp.pid_b) for sp in current_pairs
        }

        passes = 0
        while passes < MAX_PASSES:
            found_improvement = False

            i = 0
            while i < len(current_pairs) and not found_improvement:
                j = i + 1
                while j < len(current_pairs) and not found_improvement:
                    A = current_pairs[i].pid_a
                    B = current_pairs[i].pid_b
                    C = current_pairs[j].pid_a
                    D = current_pairs[j].pid_b

                    original_score = _score(A, B) + _score(C, D)

                    new1 = _canon(A, C)
                    new2 = _canon(B, D)

                    swap1_legal = (
                        new1[0] != new1[1]
                        and new2[0] != new2[1]
                        and not _in_other_rounds(new1, current_round_set)
                        and not _in_other_rounds(new2, current_round_set)
                    )

                    if swap1_legal:
                        new_score = _score(A, C) + _score(B, D)
                        if new_score > original_score + 1e-9:
                            old1 = _canon(A, B)
                            old2 = _canon(C, D)

                            all_pairs.discard(old1)
                            all_pairs.discard(old2)
                            all_pairs.add(new1)
                            all_pairs.add(new2)

                            current_round_set.discard(old1)
                            current_round_set.discard(old2)
                            current_round_set.add(new1)
                            current_round_set.add(new2)

                            current_pairs[i] = SchedulePair(
                                pid_a=new1[0],
                                pid_b=new1[1],
                                score=_score(new1[0], new1[1]),
                            )
                            current_pairs[j] = SchedulePair(
                                pid_a=new2[0],
                                pid_b=new2[1],
                                score=_score(new2[0], new2[1]),
                            )

                            found_improvement = True
                            break

                    if not found_improvement:
                        new1 = _canon(A, D)
                        new2 = _canon(B, C)

                        swap2_legal = (
                            new1[0] != new1[1]
                            and new2[0] != new2[1]
                            and not _in_other_rounds(new1, current_round_set)
                            and not _in_other_rounds(new2, current_round_set)
                        )

                        if swap2_legal:
                            new_score = _score(A, D) + _score(B, C)
                            if new_score > original_score + 1e-9:
                                old1 = _canon(A, B)
                                old2 = _canon(C, D)

                                all_pairs.discard(old1)
                                all_pairs.discard(old2)
                                all_pairs.add(new1)
                                all_pairs.add(new2)

                                current_round_set.discard(old1)
                                current_round_set.discard(old2)
                                current_round_set.add(new1)
                                current_round_set.add(new2)

                                current_pairs[i] = SchedulePair(
                                    pid_a=new1[0],
                                    pid_b=new1[1],
                                    score=_score(new1[0], new1[1]),
                                )
                                current_pairs[j] = SchedulePair(
                                    pid_a=new2[0],
                                    pid_b=new2[1],
                                    score=_score(new2[0], new2[1]),
                                )

                                found_improvement = True
                                break

                    j += 1
                i += 1

            if not found_improvement:
                break

            passes += 1

        improved_rounds.append(
            ScheduleRound(
                number=rnd.number,
                pairs=tuple(sorted(current_pairs, key=lambda sp: (sp.pid_a, sp.pid_b))),
                unmatched_pid=rnd.unmatched_pid,
            )
        )

    return GeneratedSchedule(rounds=tuple(improved_rounds))


@dataclass
class ScheduleViolation:
    kind: str
    detail: str


def validate_schedule(
    schedule: GeneratedSchedule,
    all_participant_pids: frozenset[str],
    num_rounds: int,
) -> list[ScheduleViolation]:
    violations: list[ScheduleViolation] = []

    if len(schedule.rounds) != num_rounds:
        violations.append(
            ScheduleViolation(
                kind="WRONG_ROUND_COUNT",
                detail=f"Expected {num_rounds} rounds, got {len(schedule.rounds)}.",
            )
        )

    expected_numbers = set(range(1, num_rounds + 1))
    actual_numbers = {r.number for r in schedule.rounds}
    if actual_numbers != expected_numbers:
        violations.append(
            ScheduleViolation(
                kind="WRONG_ROUND_NUMBERS",
                detail=f"Expected round numbers {sorted(expected_numbers)}, got {sorted(actual_numbers)}.",
            )
        )

    N = len(all_participant_pids)
    expected_pairs_per_round = N // 2 if N % 2 == 0 else (N - 1) // 2

    if N % 2 == 0:
        for r in schedule.rounds:
            if r.unmatched_pid is not None:
                violations.append(
                    ScheduleViolation(
                        kind="UNMATCHED_IN_EVEN_EVENT",
                        detail=f"Round {r.number} has unmatched_pid={r.unmatched_pid!r} but N is even.",
                    )
                )
    else:
        for r in schedule.rounds:
            if r.unmatched_pid is None:
                violations.append(
                    ScheduleViolation(
                        kind="MISSING_UNMATCHED",
                        detail=f"Round {r.number} missing unmatched_pid but N is odd.",
                    )
                )

    unmatched_seen: dict[str, int] = {}
    pair_seen_round: dict[tuple[str, str], int] = {}

    for r in schedule.rounds:
        if r.unmatched_pid is not None:
            if r.unmatched_pid not in all_participant_pids:
                violations.append(
                    ScheduleViolation(
                        kind="UNKNOWN_PARTICIPANT",
                        detail=f"Round {r.number} has unknown unmatched_pid={r.unmatched_pid}.",
                    )
                )
            unmatched_seen[r.unmatched_pid] = unmatched_seen.get(r.unmatched_pid, 0) + 1

        if len(r.pairs) != expected_pairs_per_round:
            violations.append(
                ScheduleViolation(
                    kind="WRONG_PAIR_COUNT",
                    detail=f"Round {r.number} expected {expected_pairs_per_round} pairs, got {len(r.pairs)}.",
                )
            )

        used_in_round: set[str] = set()

        for sp in r.pairs:
            if sp.pid_a not in all_participant_pids:
                violations.append(
                    ScheduleViolation(
                        kind="UNKNOWN_PARTICIPANT",
                        detail=f"Round {r.number} has unknown pid_a={sp.pid_a}.",
                    )
                )
            if sp.pid_b not in all_participant_pids:
                violations.append(
                    ScheduleViolation(
                        kind="UNKNOWN_PARTICIPANT",
                        detail=f"Round {r.number} has unknown pid_b={sp.pid_b}.",
                    )
                )

            if sp.pid_a == sp.pid_b:
                violations.append(
                    ScheduleViolation(
                        kind="SELF_PAIR",
                        detail=f"Round {r.number} has self-pair {sp.pid_a}.",
                    )
                )
            else:
                if _uuid_mod.UUID(sp.pid_a) >= _uuid_mod.UUID(sp.pid_b):
                    violations.append(
                        ScheduleViolation(
                            kind="WRONG_CANONICAL_ORDER",
                            detail=f"Round {r.number} has non-canonical order ({sp.pid_a}, {sp.pid_b}).",
                        )
                    )

            if sp.pid_a in used_in_round or sp.pid_b in used_in_round:
                violations.append(
                    ScheduleViolation(
                        kind="DUPLICATE_IN_ROUND",
                        detail=f"Round {r.number} has participant in multiple pairs.",
                    )
                )
            used_in_round.add(sp.pid_a)
            used_in_round.add(sp.pid_b)

            if (
                sp.score < 0.0
                or sp.score > 1.0
                or math.isnan(sp.score)
                or math.isinf(sp.score)
            ):
                violations.append(
                    ScheduleViolation(
                        kind="SCORE_OUT_OF_RANGE",
                        detail=f"Round {r.number} pair ({sp.pid_a},{sp.pid_b}) has score={sp.score!r}.",
                    )
                )

            key = _canon(sp.pid_a, sp.pid_b)
            if key in pair_seen_round and pair_seen_round[key] != r.number:
                violations.append(
                    ScheduleViolation(
                        kind="REPEATED_PARTNER",
                        detail=f"Pair {key} repeats in rounds {pair_seen_round[key]} and {r.number}.",
                    )
                )
            else:
                pair_seen_round[key] = r.number

    for pid, count in unmatched_seen.items():
        if count > 1:
            violations.append(
                ScheduleViolation(
                    kind="UNMATCHED_TWICE",
                    detail=f"Participant {pid} unmatched {count} times.",
                )
            )

    return violations
