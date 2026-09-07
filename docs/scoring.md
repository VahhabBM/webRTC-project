# Scoring

## Overview

The matching engine needs a deterministic way to quantify how well two
participants “match”. For T-19, the score is based solely on **tag overlap**.

This module provides:

- A DB-free scoring function (`compute_match_score`) that can be imported and
  tested without Django.
- Thin DB↔domain adapter helpers to convert ORM models into the normalized
  inputs expected by the scoring function.

## Input contract

### `ParticipantProfile`

`ParticipantProfile` is normalized participant data.

- `tag_ids: frozenset[str]`
  - An immutable set of **opaque identifiers**.
  - In production these are typically tag UUID strings from the database.
  - In tests they can be any strings; the scoring only depends on set
    membership.

### `ScoringWeights`

`ScoringWeights` represents event-level configuration.

- `tag_weight: float`
  - Controls how much tag overlap contributes to the final score.
  - Must be a **finite**, **non-negative** number.
  - Default is `1.0`.

## Output contract

`compute_match_score(...)` returns a `float` score in `[0.0, 1.0]`.

There are three regimes:

- **Zero overlap** → score `0.0`
- **Partial overlap** → score between `0.0` and `1.0`
- **Full overlap (identical tag sets)** → score `tag_weight` (clamped to `1.0`)

## Formula

The tag overlap signal is **Jaccard similarity**:

- Let `A` be the set of tags for participant A
- Let `B` be the set of tags for participant B

Jaccard similarity:

`jaccard(A, B) = |A ∩ B| / |A ∪ B|`

Final score:

`score = clamp(tag_weight × jaccard(A, B), 0.0, 1.0)`

## Event configuration

Events store scoring configuration in `Event.scoring_weights` (a JSON field).

- Key: `"tag_overlap"`
- Default: `1.0`

Example:

```json
{
  "tag_overlap": 1.0
}
```

## Edge cases

- Both tag sets empty (`A ∪ B` is empty) → score is `0.0`.
- One tag set empty → score is `0.0`.
- Identical sets → score is `tag_weight` (then clamped into `[0.0, 1.0]`).

## DB/domain boundary

The scoring function is intentionally “pure”: it takes `ParticipantProfile` and
`ScoringWeights` and returns a score with no DB access.

The adapter helpers in `apps/events/scoring.py` bridge the ORM to these types:

- `build_participant_profile(participant)`
  - Reads `participant.tags.all()` and returns `ParticipantProfile`.
  - For bulk scoring, ensure tags are prefetched.

- `build_scoring_weights(event)`
  - Converts `event.scoring_weights` (dict) into validated `ScoringWeights`.
  - Falls back to defaults when missing.

## T-20 usage pattern

In T-20 (bulk matching), the recommended pattern is:

1. Fetch participants and prefetch tags once.
2. Convert each ORM participant to a `ParticipantProfile` once.
3. Compute scores in a tight loop using only pure Python objects.

Pseudocode:

```text
participants = Participant.objects.filter(event=event).prefetch_related("tags")
weights = build_scoring_weights(event)
profiles = {p.pk: build_participant_profile(p) for p in participants}

for each (a_id, b_id) pair to consider:
    score = compute_match_score(profiles[a_id], profiles[b_id], weights)
    ... use score to select pairs ...
```
