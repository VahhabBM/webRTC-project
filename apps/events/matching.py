from __future__ import annotations

import math
import uuid as _uuid_mod
from dataclasses import dataclass
from functools import lru_cache

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
    gender: str | None = None
    want_to_date: str | None = None
    max_dates: int | None = None


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
    unmatched_pids: tuple[str, ...] = ()

    @property
    def all_unmatched_pids(self) -> tuple[str, ...]:
        if self.unmatched_pids:
            return self.unmatched_pids
        return (self.unmatched_pid,) if self.unmatched_pid else ()


@dataclass(frozen=True)
class GeneratedSchedule:
    rounds: tuple[ScheduleRound, ...]


@lru_cache(maxsize=16384)
def _uuid_value(pid: str) -> _uuid_mod.UUID:
    return _uuid_mod.UUID(pid)


def _canon(x: str, y: str) -> tuple[str, str]:
    """Return (x, y) or (y, x) such that uuid.UUID(result[0]) < uuid.UUID(result[1])."""

    return (x, y) if _uuid_value(x) < _uuid_value(y) else (y, x)


def _normalized_gender(value: str | None) -> str:
    if value == "Male":
        return "Male"
    if value == "Female":
        return "Female"
    return "Other"


def _accepts(participant: MatchParticipant, partner: MatchParticipant) -> bool:
    preference = participant.want_to_date or "Any"
    return preference == "Any" or preference == _normalized_gender(partner.gender)


def _participant_limit(participant: MatchParticipant) -> int:
    if participant.max_dates is not None:
        return max(0, int(participant.max_dates))
    return 7 if _normalized_gender(participant.gender) == "Female" else 5


def _generate_constrained_schedule(match_input: MatchInput) -> GeneratedSchedule:
    participants = tuple(sorted(match_input.participants, key=lambda p: _uuid_value(p.pid)))
    by_pid = {p.pid: p for p in participants}
    adjacency: dict[str, list[str]] = {p.pid: [] for p in participants}
    scores: dict[tuple[str, str], float] = {}

    for index, participant in enumerate(participants):
        for partner in participants[index + 1 :]:
            if _accepts(participant, partner) and _accepts(partner, participant):
                key = _canon(participant.pid, partner.pid)
                adjacency[participant.pid].append(partner.pid)
                adjacency[partner.pid].append(participant.pid)
                scores[key] = compute_match_score(
                    participant.profile, partner.profile, match_input.weights
                )

    for pid, candidates in adjacency.items():
        candidates.sort(key=_uuid_value)

    is_female_male_bipartite = bool(scores) and all(
        {_normalized_gender(by_pid[a].gender), _normalized_gender(by_pid[b].gender)}
        == {"Female", "Male"}
        for a, b in scores
    )
    pair_counts = {p.pid: 0 for p in participants}
    last_round = {p.pid: 0 for p in participants}
    used_pairs: set[tuple[str, str]] = set()
    rounds: list[ScheduleRound] = []

    for round_number in range(1, match_input.num_rounds + 1):
        available = {
            p.pid
            for p in participants
            if pair_counts[p.pid] < _participant_limit(p)
        }
        eligible_edges = {
            pid: [
                other
                for other in adjacency[pid]
                if other in available and _canon(pid, other) not in used_pairs
            ]
            for pid in available
        }

        def partner_key(pid: str, other: str) -> tuple:
            return (
                pair_counts[other],
                last_round[other],
                -scores[_canon(pid, other)],
                _uuid_value(other),
            )

        round_pairs: list[tuple[str, str]] = []
        if is_female_male_bipartite:
            women = [
                p.pid
                for p in participants
                if p.pid in available and _normalized_gender(p.gender) == "Female"
            ]
            men = {
                p.pid
                for p in participants
                if p.pid in available and _normalized_gender(p.gender) == "Male"
            }
            women.sort(
                key=lambda pid: (
                    len([m for m in eligible_edges[pid] if m in men]),
                    pair_counts[pid],
                    last_round[pid],
                    _uuid_value(pid),
                )
            )
            assigned_men: dict[str, str] = {}
            ranked_men = {
                woman_id: sorted(
                    (man_id for man_id in eligible_edges[woman_id] if man_id in men),
                    key=lambda man_id: partner_key(woman_id, man_id),
                )
                for woman_id in women
            }
            infinity = len(women) + 1
            distance: dict[str, int] = {}

            def breadth_first_layers() -> int:
                queue: list[str] = []
                assigned_women = set(assigned_men.values())
                for woman_id in women:
                    if woman_id not in assigned_women:
                        distance[woman_id] = 0
                        queue.append(woman_id)
                    else:
                        distance[woman_id] = infinity
                shortest_augmenting_path = infinity
                cursor = 0
                while cursor < len(queue):
                    woman_id = queue[cursor]
                    cursor += 1
                    if distance[woman_id] >= shortest_augmenting_path:
                        continue
                    for man_id in ranked_men[woman_id]:
                        previous_woman = assigned_men.get(man_id)
                        if previous_woman is None:
                            shortest_augmenting_path = distance[woman_id] + 1
                        elif distance[previous_woman] == infinity:
                            distance[previous_woman] = distance[woman_id] + 1
                            queue.append(previous_woman)
                return shortest_augmenting_path

            def depth_first_augment(woman_id: str, shortest_path: int) -> bool:
                for man_id in ranked_men[woman_id]:
                    previous_woman = assigned_men.get(man_id)
                    if previous_woman is None:
                        if distance[woman_id] + 1 != shortest_path:
                            continue
                    elif (
                        distance[previous_woman] != distance[woman_id] + 1
                        or not depth_first_augment(previous_woman, shortest_path)
                    ):
                        continue
                    assigned_men[man_id] = woman_id
                    return True
                distance[woman_id] = infinity
                return False

            while True:
                shortest_path = breadth_first_layers()
                if shortest_path == infinity:
                    break
                assigned_women = set(assigned_men.values())
                for woman_id in women:
                    if woman_id not in assigned_women:
                        depth_first_augment(woman_id, shortest_path)
            round_pairs = [
                _canon(woman_id, man_id)
                for man_id, woman_id in assigned_men.items()
            ]
        else:
            unmatched = set(available)
            remaining_degree = {
                pid: len(eligible_edges[pid]) for pid in available
            }
            ranked_partners = {
                pid: sorted(
                    eligible_edges[pid],
                    key=lambda other: partner_key(pid, other),
                )
                for pid in available
            }

            def remove_from_round(pid: str) -> None:
                unmatched.remove(pid)
                for other in eligible_edges[pid]:
                    if other in unmatched:
                        remaining_degree[other] -= 1

            while unmatched:
                pid = min(
                    unmatched,
                    key=lambda candidate: (
                        remaining_degree[candidate],
                        pair_counts[candidate],
                        last_round[candidate],
                        _uuid_value(candidate),
                    ),
                )
                partner = next(
                    (other for other in ranked_partners[pid] if other in unmatched),
                    None,
                )
                if partner is None:
                    remove_from_round(pid)
                    continue
                round_pairs.append(_canon(pid, partner))
                remove_from_round(pid)
                remove_from_round(partner)

        paired_ids: set[str] = set()
        pairs_out: list[SchedulePair] = []
        for pid_a, pid_b in sorted(round_pairs, key=lambda pair: (_uuid_value(pair[0]), _uuid_value(pair[1]))):
            paired_ids.update((pid_a, pid_b))
            pair_key = (pid_a, pid_b)
            used_pairs.add(pair_key)
            pair_counts[pid_a] += 1
            pair_counts[pid_b] += 1
            last_round[pid_a] = round_number
            last_round[pid_b] = round_number
            pairs_out.append(
                SchedulePair(pid_a=pid_a, pid_b=pid_b, score=scores[pair_key])
            )

        unmatched_pids = tuple(p.pid for p in participants if p.pid not in paired_ids)
        rounds.append(
            ScheduleRound(
                number=round_number,
                pairs=tuple(pairs_out),
                unmatched_pid=unmatched_pids[0] if len(unmatched_pids) == 1 else None,
                unmatched_pids=unmatched_pids,
            )
        )

    return GeneratedSchedule(rounds=tuple(rounds))


def generate_schedule(match_input: MatchInput) -> GeneratedSchedule:
    """Generate an R-round no-repeat pairing schedule entirely in memory."""

    pid_list: list[str] = [p.pid for p in match_input.participants]
    if match_input.num_rounds < 1:
        raise MatchingImpossibleError("num_rounds must be at least 1.")
    if len(set(pid_list)) != len(pid_list):
        raise MatchingError("Participant IDs must be unique.")
    try:
        for pid in pid_list:
            _uuid_value(pid)
    except (ValueError, AttributeError, TypeError) as exc:
        raise MatchingError("Participant IDs must be valid UUID strings.") from exc
    has_metadata = any(
        p.gender is not None or p.want_to_date is not None or p.max_dates is not None
        for p in match_input.participants
    )
    unrestricted = all(
        (p.want_to_date or "Any") == "Any"
        and _participant_limit(p) >= match_input.num_rounds
        for p in match_input.participants
    )
    if has_metadata and not unrestricted:
        if len(pid_list) < 2:
            raise MatchingImpossibleError(
                f"Cannot generate a schedule with fewer than 2 participants (got {len(pid_list)})."
            )
        max_rounds = len(pid_list) - 1 if len(pid_list) % 2 == 0 else len(pid_list)
        if match_input.num_rounds > max_rounds:
            raise MatchingImpossibleError(
                f"Participant count {len(pid_list)} is too small for "
                f"{match_input.num_rounds} non-repeating rounds. "
                f"At most {max_rounds} rounds are possible."
            )
        return _generate_constrained_schedule(match_input)

    profiles: dict[str, ParticipantProfile] = {
        p.pid: p.profile for p in match_input.participants
    }
    weights = match_input.weights

    # Assign stable integer rank 0..N-1 sorted by UUID value.
    # i < j  <=>  _uuid_value(idx_to_pid[i]) < _uuid_value(idx_to_pid[j])
    #         <=>  idx_to_pid[i] < idx_to_pid[j]  (UUID string order == UUID int order)
    # Integer comparison is therefore a drop-in replacement for UUID string tiebreaking.
    idx_to_pid: list[str] = sorted(pid_list, key=_uuid_value)

    N = len(pid_list)
    R = match_input.num_rounds

    if N < 2:
        raise MatchingImpossibleError(
            f"Cannot generate a schedule with fewer than 2 participants (got {N})."
        )

    max_rounds = N - 1 if N % 2 == 0 else N
    if R > max_rounds:
        raise MatchingImpossibleError(
            f"Participant count {N} is too small for {R} non-repeating rounds. "
            f"At most {max_rounds} rounds are possible."
        )

    # Build symmetric score matrix indexed by integer rank.
    score_mat: list[list[float]] = [[0.0] * N for _ in range(N)]
    for i in range(N):
        for j in range(i + 1, N):
            s = compute_match_score(
                profiles[idx_to_pid[i]], profiles[idx_to_pid[j]], weights
            )
            score_mat[i][j] = s
            score_mat[j][i] = s

    # Precompute sorted neighbor lists for greedy best-pick.
    # sorted_by_score[i] = list of j (j != i) sorted by (-score_mat[i][j], j).
    # Iterating this and taking the first j in `remaining` and not in `forbidden_i[i]`
    # is equivalent to sorting filtered candidates each time but avoids repeated sorting.
    sorted_by_score: list[list[int]] = [
        sorted(
            (j for j in range(N) if j != i),
            key=lambda j, i=i: (-score_mat[i][j], j),
        )
        for i in range(N)
    ]

    forbidden_i: list[set[int]] = [set() for _ in range(N)]
    unmatched_count: list[int] = [0] * N

    # Parallel lists built during greedy phase.
    rounds_pairs_i: list[list[tuple[int, int]]] = []
    rounds_bye_i: list[int | None] = []

    for round_num in range(1, R + 1):
        if N % 2 == 1:
            eligible_i = [i for i in range(N) if unmatched_count[i] == 0]
            if not eligible_i:
                raise MatchingImpossibleError(
                    "Cannot satisfy 'no participant unmatched more than once' in "
                    f"round {round_num}: all participants have already been unmatched."
                )

            # avail_for_bye[i] = (N-1) - len(forbidden_i[i]) because forbidden_i[i]
            # contains only distinct participants != i, so this is O(1) and correct.
            bye_idx: int | None = max(
                eligible_i, key=lambda i: (N - 1 - len(forbidden_i[i]), i)
            )

            unmatched_count[bye_idx] += 1
            working_set_i: set[int] = set(range(N)) - {bye_idx}
        else:
            bye_idx = None
            working_set_i = set(range(N))

        round_pairs_i: list[tuple[int, int]] = []
        forbidden_before = [set(s) for s in forbidden_i]
        failed_idx: int | None = None
        remaining: set[int] = set(working_set_i)
        available_count_i: dict[int, int] = {
            i: sum(j != i and j not in forbidden_i[i] for j in remaining)
            for i in remaining
        }

        while remaining:
            pid_i = min(
                remaining,
                key=lambda candidate: (available_count_i[candidate], candidate),
            )

            best_i: int | None = None
            for j in sorted_by_score[pid_i]:
                if j in remaining and j not in forbidden_i[pid_i]:
                    best_i = j
                    break

            if best_i is None:
                failed_idx = pid_i
                break

            a_idx, b_idx = (pid_i, best_i) if pid_i < best_i else (best_i, pid_i)
            round_pairs_i.append((a_idx, b_idx))
            remaining.remove(pid_i)
            remaining.remove(best_i)
            for candidate in remaining:
                if pid_i not in forbidden_i[candidate]:
                    available_count_i[candidate] -= 1
                if best_i not in forbidden_i[candidate]:
                    available_count_i[candidate] -= 1

            forbidden_i[pid_i].add(best_i)
            forbidden_i[best_i].add(pid_i)

        if failed_idx is not None:
            # Deterministic recovery for a greedy dead end.  This is only a
            # fallback; normal operation remains hardest-first greedy.  The
            # search is bounded to the unresolved round and is practical for
            # the small pathological cases where greedy gets stuck.
            for i in range(N):
                forbidden_i[i] = forbidden_before[i]

            recovery_nodes = 0
            max_recovery_nodes = 10_000

            def search(remaining_s: set[int], chosen: list[tuple[int, int]]) -> bool:
                nonlocal recovery_nodes
                recovery_nodes += 1
                if recovery_nodes > max_recovery_nodes:
                    return False
                if not remaining_s:
                    return True
                pid_r = min(
                    remaining_s,
                    key=lambda candidate: (
                        len((remaining_s - {candidate}) - forbidden_i[candidate]),
                        candidate,
                    ),
                )
                candidates_r = [
                    j
                    for j in sorted_by_score[pid_r]
                    if j in remaining_s and j not in forbidden_i[pid_r]
                ]
                for partner in candidates_r:
                    forbidden_i[pid_r].add(partner)
                    forbidden_i[partner].add(pid_r)
                    chosen.append((pid_r, partner))
                    if search(remaining_s - {pid_r, partner}, chosen):
                        return True
                    chosen.pop()
                    forbidden_i[pid_r].remove(partner)
                    forbidden_i[partner].remove(pid_r)
                return False

            recovered: list[tuple[int, int]] = []
            if not search(set(working_set_i), recovered):
                raise MatchingImpossibleError(
                    f"Unable to construct a valid matching in round {round_num}; "
                    f"participant {idx_to_pid[failed_idx]} became stranded after greedy choices "
                    f"and bounded recovery examined {recovery_nodes} states."
                )
            round_pairs_i = [(min(a, b), max(a, b)) for a, b in recovered]

        rounds_pairs_i.append(round_pairs_i)
        rounds_bye_i.append(bye_idx)

    MAX_PASSES = 10

    all_pairs_i: set[tuple[int, int]] = {
        pair for rnd_pairs in rounds_pairs_i for pair in rnd_pairs
    }

    for r_idx in range(R):
        current_pairs: list[tuple[int, int]] = list(rounds_pairs_i[r_idx])
        current_round_set: set[tuple[int, int]] = set(current_pairs)

        passes = 0
        while passes < MAX_PASSES:
            found_improvement = False

            i = 0
            while i < len(current_pairs) and not found_improvement:
                ai, bi = current_pairs[i]
                j = i + 1
                while j < len(current_pairs) and not found_improvement:
                    ci, di = current_pairs[j]

                    original_score = score_mat[ai][bi] + score_mat[ci][di]

                    # Swap 1: (ai,ci) and (bi,di)
                    n1 = (ai, ci) if ai < ci else (ci, ai)
                    n2 = (bi, di) if bi < di else (di, bi)

                    swap1_legal = (
                        n1[0] != n1[1]
                        and n2[0] != n2[1]
                        and (n1 not in all_pairs_i or n1 in current_round_set)
                        and (n2 not in all_pairs_i or n2 in current_round_set)
                    )

                    if swap1_legal:
                        new_score = score_mat[n1[0]][n1[1]] + score_mat[n2[0]][n2[1]]
                        if new_score > original_score + 1e-9:
                            old1 = (ai, bi)
                            old2 = (ci, di)

                            all_pairs_i.discard(old1)
                            all_pairs_i.discard(old2)
                            all_pairs_i.add(n1)
                            all_pairs_i.add(n2)

                            current_round_set.discard(old1)
                            current_round_set.discard(old2)
                            current_round_set.add(n1)
                            current_round_set.add(n2)

                            current_pairs[i] = n1
                            current_pairs[j] = n2

                            found_improvement = True
                            break

                    if not found_improvement:
                        # Swap 2: (ai,di) and (bi,ci)
                        n1 = (ai, di) if ai < di else (di, ai)
                        n2 = (bi, ci) if bi < ci else (ci, bi)

                        swap2_legal = (
                            n1[0] != n1[1]
                            and n2[0] != n2[1]
                            and (n1 not in all_pairs_i or n1 in current_round_set)
                            and (n2 not in all_pairs_i or n2 in current_round_set)
                        )

                        if swap2_legal:
                            new_score = (
                                score_mat[n1[0]][n1[1]] + score_mat[n2[0]][n2[1]]
                            )
                            if new_score > original_score + 1e-9:
                                old1 = (ai, bi)
                                old2 = (ci, di)

                                all_pairs_i.discard(old1)
                                all_pairs_i.discard(old2)
                                all_pairs_i.add(n1)
                                all_pairs_i.add(n2)

                                current_round_set.discard(old1)
                                current_round_set.discard(old2)
                                current_round_set.add(n1)
                                current_round_set.add(n2)

                                current_pairs[i] = n1
                                current_pairs[j] = n2

                                found_improvement = True
                                break

                    j += 1
                i += 1

            if not found_improvement:
                break

            passes += 1

        rounds_pairs_i[r_idx] = current_pairs

    # Convert integer-indexed rounds back to public ScheduleRound objects.
    improved_rounds: list[ScheduleRound] = []
    for r_idx in range(R):
        bye_int = rounds_bye_i[r_idx]
        # Sort by integer index (equivalent to UUID string sort since idx_to_pid is UUID-sorted).
        pairs_sorted = sorted(rounds_pairs_i[r_idx])
        pairs_out = tuple(
            SchedulePair(
                pid_a=idx_to_pid[ai],
                pid_b=idx_to_pid[bi],
                score=score_mat[ai][bi],
            )
            for ai, bi in pairs_sorted
        )
        improved_rounds.append(
            ScheduleRound(
                number=r_idx + 1,
                pairs=pairs_out,
                unmatched_pid=idx_to_pid[bye_int] if bye_int is not None else None,
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
    match_input: MatchInput | None = None,
) -> list[ScheduleViolation]:
    if match_input and any(
        p.gender is not None or p.want_to_date is not None or p.max_dates is not None
        for p in match_input.participants
    ):
        return _validate_constrained_schedule(
            schedule, all_participant_pids, num_rounds, match_input
        )

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
                try:
                    noncanonical = _uuid_mod.UUID(sp.pid_a) >= _uuid_mod.UUID(sp.pid_b)
                except (ValueError, AttributeError, TypeError):
                    noncanonical = False
                if noncanonical:
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

            try:
                invalid_score = (
                    not isinstance(sp.score, (int, float))
                    or math.isnan(sp.score)
                    or math.isinf(sp.score)
                    or sp.score < 0.0
                    or sp.score > 1.0
                )
            except (TypeError, ValueError):
                invalid_score = True
            if invalid_score:
                violations.append(
                    ScheduleViolation(
                        kind="SCORE_OUT_OF_RANGE",
                        detail=f"Round {r.number} pair ({sp.pid_a},{sp.pid_b}) has score={sp.score!r}.",
                    )
                )

            try:
                key = _canon(sp.pid_a, sp.pid_b)
            except (ValueError, AttributeError, TypeError):
                continue
            if key in pair_seen_round and pair_seen_round[key] != r.number:
                violations.append(
                    ScheduleViolation(
                        kind="REPEATED_PARTNER",
                        detail=f"Pair {key} repeats in rounds {pair_seen_round[key]} and {r.number}.",
                    )
                )
            else:
                pair_seen_round[key] = r.number

        if r.unmatched_pid is not None and r.unmatched_pid in used_in_round:
            violations.append(
                ScheduleViolation(
                    kind="UNMATCHED_PAIRED",
                    detail=f"Round {r.number} unmatched participant {r.unmatched_pid} is also paired.",
                )
            )

    for pid, count in unmatched_seen.items():
        if count > 1:
            violations.append(
                ScheduleViolation(
                    kind="UNMATCHED_TWICE",
                    detail=f"Participant {pid} unmatched {count} times.",
                )
            )

    return violations


def _validate_constrained_schedule(
    schedule: GeneratedSchedule,
    all_participant_pids: frozenset[str],
    num_rounds: int,
    match_input: MatchInput,
) -> list[ScheduleViolation]:
    violations: list[ScheduleViolation] = []
    participants = {participant.pid: participant for participant in match_input.participants}
    pair_counts = {pid: 0 for pid in all_participant_pids}
    pair_rounds: dict[tuple[str, str], int] = {}

    if len(schedule.rounds) != num_rounds:
        violations.append(
            ScheduleViolation(
                "WRONG_ROUND_COUNT",
                f"Expected {num_rounds} rounds, got {len(schedule.rounds)}.",
            )
        )
    if {rnd.number for rnd in schedule.rounds} != set(range(1, num_rounds + 1)):
        violations.append(
            ScheduleViolation("WRONG_ROUND_NUMBERS", "Round numbers are incomplete or duplicated.")
        )

    for rnd in schedule.rounds:
        used: set[str] = set()
        for pair in rnd.pairs:
            if pair.pid_a not in all_participant_pids or pair.pid_b not in all_participant_pids:
                violations.append(
                    ScheduleViolation("UNKNOWN_PARTICIPANT", f"Round {rnd.number} contains an unknown participant.")
                )
                continue
            if pair.pid_a == pair.pid_b:
                violations.append(
                    ScheduleViolation("SELF_PAIR", f"Round {rnd.number} contains a self-pair.")
                )
                continue
            if _uuid_value(pair.pid_a) >= _uuid_value(pair.pid_b):
                violations.append(
                    ScheduleViolation("WRONG_CANONICAL_ORDER", f"Round {rnd.number} contains an unordered pair.")
                )
            if pair.pid_a in used or pair.pid_b in used:
                violations.append(
                    ScheduleViolation("DUPLICATE_IN_ROUND", f"Round {rnd.number} uses a participant more than once.")
                )
            used.update((pair.pid_a, pair.pid_b))
            key = (pair.pid_a, pair.pid_b)
            if key in pair_rounds:
                violations.append(
                    ScheduleViolation("REPEATED_PARTNER", f"A pair repeats in rounds {pair_rounds[key]} and {rnd.number}.")
                )
            else:
                pair_rounds[key] = rnd.number
            pair_counts[pair.pid_a] += 1
            pair_counts[pair.pid_b] += 1

            first = participants.get(pair.pid_a)
            second = participants.get(pair.pid_b)
            if first and second and not (
                _accepts(first, second) and _accepts(second, first)
            ):
                violations.append(
                    ScheduleViolation("PREFERENCE_VIOLATION", f"Round {rnd.number} violates mutual date preferences.")
                )
            if not isinstance(pair.score, (int, float)) or not math.isfinite(pair.score) or not 0 <= pair.score <= 1:
                violations.append(
                    ScheduleViolation("SCORE_OUT_OF_RANGE", f"Round {rnd.number} contains an invalid score.")
                )

        expected_unmatched = set(all_participant_pids) - used
        actual_unmatched = set(rnd.all_unmatched_pids)
        if expected_unmatched != actual_unmatched:
            violations.append(
                ScheduleViolation("WRONG_UNMATCHED_SET", f"Round {rnd.number} does not report all unmatched participants.")
            )
        if used & actual_unmatched:
            violations.append(
                ScheduleViolation("UNMATCHED_PAIRED", f"Round {rnd.number} marks a paired participant unmatched.")
            )

    for pid, count in pair_counts.items():
        participant = participants.get(pid)
        if participant and count > _participant_limit(participant):
            violations.append(
                ScheduleViolation("DATE_LIMIT_EXCEEDED", f"Participant {pid} exceeds their configured date limit.")
            )

    return violations
