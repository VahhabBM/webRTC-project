# Matching Engine (T-20)

## Overview

T-20 adds an in-memory matching engine that generates an **R-round** schedule of
pairings with hard constraints:

- no repeated partner across rounds
- no participant appears in more than one pair in the same round
- if participant count is odd: exactly one unmatched (bye) per round, and no
  participant receives a bye more than once

The engine is pure Python and deterministic.

## Algorithm

### Score precomputation

At the start of schedule generation, all pairwise match scores are precomputed
once using the existing T-19 scoring API (`compute_match_score`). This produces
an `O(N^2)` score cache for constant-time lookup during matching.

### Greedy hardest-first assignment

Each round is generated with a greedy algorithm.

#### "Hardest first" defined

Participants are processed in ascending order of how constrained they are in the
current round.

#### "Available partner" defined

For a participant `pid`, the available partner count is:

`len((working_set - {pid}) - forbidden[pid])`

where `forbidden[pid]` is the set of participants already matched with `pid` in
prior rounds.

### Unmatched (bye) participant selection

If `N` is odd, exactly one participant is excluded from the working set each
round.

- Eligible candidates are those who have not yet been unmatched.
- The bye is chosen as the eligible participant with the **most** remaining
  available partners (most flexible to exclude).
- Tie-break is lexicographically largest pid string for determinism.

### No-repeat enforcement

A cross-round `forbidden` mapping is maintained:

- `forbidden[pid]` contains all partners already paired with `pid` in previous
  rounds.
- After each pair is chosen, both directions are recorded.

### Local improvement (first-improvement with restart)

After all greedy rounds are built, each round is improved with a local search.

- Iterate all pair-combinations `(i, j)` with `i < j`.
- Try two swap types in order:
  1. `(A, C) + (B, D)`
  2. `(A, D) + (B, C)`
- Accept the **first** swap that strictly improves combined score.
- After accepting a swap, immediately restart scanning from the beginning.

#### Swap legality

A candidate swap is legal iff:

- neither resulting pair already exists in any other round
- neither resulting pair is a self-pair

#### Termination condition

Per round:

- stop when a full scan finds no improving swap, or
- after `MAX_PASSES = 10` accepted-swap passes

#### Complexity

Per round: `O(P^2 * MAX_PASSES)` where `P ≈ N/2`.

For `N=900`, `R=6`, `MAX_PASSES=10` this is designed to complete in a few
seconds on a typical developer laptop.

## T-20 six-round requirement vs. generic engine

- `apps/events/matching.py` is **generic**: it uses `MatchInput.num_rounds` and
  does not hardcode 6.
- The management command generates exactly `event.num_rounds` rounds.

T-20 acceptance tests configure events with `num_rounds=6`.

## Input and output types

The matching engine operates on domain types:

- `MatchParticipant(pid: str, profile: ParticipantProfile)`
- `MatchInput(participants, num_rounds, weights)`

It returns:

- `GeneratedSchedule(rounds=(ScheduleRound(...), ...))`

Each `SchedulePair` includes the canonical ordered `(pid_a, pid_b)` and its
precomputed score.

## Performance

### Design target (<10 s for N=900, R=6)

The intended design target is **under 10 seconds** to generate a 900-participant,
6-round schedule on a modern developer laptop.

### CI benchmark threshold (30 s)

The benchmark test uses a **30 second** assertion threshold to avoid false
failures on slower CI runners.

A result between 10s and 30s indicates a likely performance regression worth
investigating, but it should not block CI.

## Known limitations

### Minimum participant count (N >= R+1)

A necessary feasibility constraint is `N >= R+1`. If `N <= R`, schedule
construction is structurally impossible and generation fails fast.

### Concurrency (T-22 scope)

The engine itself is in-memory and does not coordinate concurrent DB writes.
Concurrency control is expected to be addressed separately.

### Greedy optimality

Greedy + local improvement is not guaranteed globally optimal.
It is designed to satisfy constraints deterministically and produce reasonably
high-scoring schedules quickly.

## Management command

The `generate_schedule` management command loads an event, builds a `MatchInput`,
generates and validates a schedule, and (unless `--dry-run`) persists it.

### Usage examples

- Generate and persist:

  `python manage.py generate_schedule <event_uuid>`

- Dry run:

  `python manage.py generate_schedule <event_uuid> --dry-run`

- Verbose round summaries:

  `python manage.py generate_schedule <event_uuid> --verbose`

### Rerun semantics

Rerunning the command for the same event replaces the schedule (it deletes all
rounds for the event and recreates rounds and pairs).

### Failure atomicity

Persistence occurs inside a single `transaction.atomic()` block. If pair
insertion fails, the transaction rolls back and the prior schedule remains
intact.

## T-19 integration

The engine integrates with T-19 strictly via:

- `build_participant_profile(participant)` (requires `prefetch_related("tags")`)
- `build_scoring_weights(event)`
- `compute_match_score(profile_a, profile_b, weights)`

No scoring logic is duplicated inside the matching engine.
