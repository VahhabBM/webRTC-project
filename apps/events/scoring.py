from __future__ import annotations

import math
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Pure domain types and function (no Django/ORM imports)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParticipantProfile:
    """Normalized, DB-free participant data for the scoring function.

    tag_ids is a frozenset of opaque string identifiers (UUID strings from
    the DB, or plain name strings in tests). The scoring function only cares
    about set membership, not the content of the strings.
    """

    tag_ids: frozenset[str]


@dataclass(frozen=True)
class ScoringWeights:
    """Event-level scoring configuration.

    tag_weight controls the contribution of tag-overlap (Jaccard similarity)
    to the final score. Must be a non-negative finite float. Default 1.0
    means the score equals raw Jaccard similarity.
    """

    tag_weight: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.tag_weight, (int, float)):
            raise ValueError(
                f"tag_weight must be a number, got {type(self.tag_weight).__name__}"
            )
        if math.isnan(self.tag_weight) or math.isinf(self.tag_weight):
            raise ValueError(
                f"tag_weight must be a finite number, got {self.tag_weight!r}"
            )
        if self.tag_weight < 0:
            raise ValueError(
                f"tag_weight must be non-negative, got {self.tag_weight!r}"
            )


def compute_match_score(
    profile_a: ParticipantProfile,
    profile_b: ParticipantProfile,
    weights: ScoringWeights,
) -> float:
    """Return a match score in [0.0, 1.0] for two participants.

    Tag overlap (Jaccard similarity) is the primary and only scoring signal
    for T-19. The result is tag_weight * jaccard, clamped to [0.0, 1.0].

    This function has no database dependency and no side effects.
    It is deterministic: identical inputs always produce the same output.

    Parameters
    ----------
    profile_a, profile_b:
        Normalized participant data. Build with build_participant_profile().
    weights:
        Event-level scoring configuration. Build with build_scoring_weights().

    Returns
    -------
    float
        A score in [0.0, 1.0]. Higher means more similar.
        0.0 when there is no tag overlap, or when both profiles have no tags.
        1.0 (× tag_weight) when the tag sets are identical.
    """

    union = profile_a.tag_ids | profile_b.tag_ids
    if not union:
        return 0.0
    intersection = profile_a.tag_ids & profile_b.tag_ids
    jaccard = len(intersection) / len(union)
    raw = weights.tag_weight * jaccard
    return min(1.0, max(0.0, raw))


# ---------------------------------------------------------------------------
# DB ↔ domain adapters
# ---------------------------------------------------------------------------
# These functions touch the ORM. Call them BEFORE entering scoring loops.
# Never call them from compute_match_score.


def build_participant_profile(participant) -> ParticipantProfile:
    """Convert an ORM Participant to a ParticipantProfile.

    Requires an active DB connection. The participant's tags must already
    be prefetched (use prefetch_related("tags")) for efficiency in bulk
    scoring loops.

    Parameters
    ----------
    participant:
        A apps.events.models.Participant instance with tags accessible.

    Returns
    -------
    ParticipantProfile
        Immutable profile ready for compute_match_score.
    """

    tag_ids = frozenset(str(tag.pk) for tag in participant.tags.all())
    return ParticipantProfile(tag_ids=tag_ids)


def build_scoring_weights(event) -> ScoringWeights:
    """Convert Event.scoring_weights dict to a ScoringWeights instance.

    Falls back to defaults if the field is None or missing the key.

    Parameters
    ----------
    event:
        A apps.events.models.Event instance.

    Returns
    -------
    ScoringWeights
        Validated configuration ready for compute_match_score.

    Raises
    ------
    ValueError
        If the stored tag_overlap value cannot be converted to a valid float.
    """

    config: dict = event.scoring_weights or {}
    tag_weight = float(config.get("tag_overlap", 1.0))
    return ScoringWeights(tag_weight=tag_weight)
