import math

import pytest

from apps.events.scoring import ParticipantProfile, ScoringWeights, compute_match_score


def test_zero_overlap_returns_zero():
    weights = ScoringWeights(tag_weight=1.0)
    a = ParticipantProfile(tag_ids=frozenset({"a", "b"}))
    b = ParticipantProfile(tag_ids=frozenset({"c", "d"}))
    assert compute_match_score(a, b, weights) == 0.0


def test_partial_overlap_is_nonzero_and_less_than_full():
    weights = ScoringWeights(tag_weight=1.0)
    a = ParticipantProfile(tag_ids=frozenset({"a", "b", "c"}))
    b = ParticipantProfile(tag_ids=frozenset({"b", "c", "d"}))
    score_partial = compute_match_score(a, b, weights)
    score_full = compute_match_score(
        ParticipantProfile(tag_ids=frozenset({"a", "b"})),
        ParticipantProfile(tag_ids=frozenset({"a", "b"})),
        weights,
    )
    assert score_partial == pytest.approx(0.5)
    assert 0.0 < score_partial < score_full


def test_full_overlap_returns_tag_weight():
    weights = ScoringWeights(tag_weight=1.0)
    a = ParticipantProfile(tag_ids=frozenset({"a", "b"}))
    b = ParticipantProfile(tag_ids=frozenset({"a", "b"}))
    assert compute_match_score(a, b, weights) == pytest.approx(1.0)


def test_ordering_zero_lt_partial_lt_full():
    weights = ScoringWeights(tag_weight=1.0)

    zero = compute_match_score(
        ParticipantProfile(tag_ids=frozenset({"a", "b"})),
        ParticipantProfile(tag_ids=frozenset({"c", "d"})),
        weights,
    )
    partial = compute_match_score(
        ParticipantProfile(tag_ids=frozenset({"a", "b", "c"})),
        ParticipantProfile(tag_ids=frozenset({"b", "c", "d"})),
        weights,
    )
    full = compute_match_score(
        ParticipantProfile(tag_ids=frozenset({"a", "b"})),
        ParticipantProfile(tag_ids=frozenset({"a", "b"})),
        weights,
    )

    assert zero < partial < full


@pytest.mark.parametrize(
    "tags_a,tags_b",
    [
        (frozenset(), frozenset()),
        (frozenset(), frozenset({"a"})),
        (frozenset({"a"}), frozenset({"a"})),
        (frozenset({"a", "b", "c"}), frozenset({"b", "c", "d"})),
        (frozenset({"a", "b", "c", "d", "e"}), frozenset({"c", "d"})),
        (
            frozenset({str(i) for i in range(50)}),
            frozenset({str(i) for i in range(25, 75)}),
        ),
    ],
)
def test_score_always_in_0_1_range(tags_a, tags_b):
    weights = ScoringWeights(tag_weight=1.0)
    score = compute_match_score(
        ParticipantProfile(tag_ids=tags_a),
        ParticipantProfile(tag_ids=tags_b),
        weights,
    )
    assert 0.0 <= score <= 1.0


def test_both_profiles_empty_returns_zero():
    weights = ScoringWeights(tag_weight=1.0)
    a = ParticipantProfile(tag_ids=frozenset())
    b = ParticipantProfile(tag_ids=frozenset())
    assert compute_match_score(a, b, weights) == 0.0


def test_one_profile_empty_returns_zero():
    weights = ScoringWeights(tag_weight=1.0)
    a = ParticipantProfile(tag_ids=frozenset({"a"}))
    b = ParticipantProfile(tag_ids=frozenset())
    assert compute_match_score(a, b, weights) == 0.0


def test_duplicate_tags_in_frozenset_deduped():
    weights = ScoringWeights(tag_weight=1.0)

    # Duplicates are removed at frozenset construction; this ensures the
    # scoring function behaves as set math expects.
    a = ParticipantProfile(tag_ids=frozenset(["a", "a", "b"]))
    b = ParticipantProfile(tag_ids=frozenset(["a", "b"]))
    assert compute_match_score(a, b, weights) == pytest.approx(1.0)


def test_configurable_weight_scales_score():
    weights = ScoringWeights(tag_weight=0.5)
    a = ParticipantProfile(tag_ids=frozenset({"a", "b"}))
    b = ParticipantProfile(tag_ids=frozenset({"a", "b"}))
    assert compute_match_score(a, b, weights) == pytest.approx(0.5)


def test_zero_weight_returns_zero():
    weights = ScoringWeights(tag_weight=0.0)
    a = ParticipantProfile(tag_ids=frozenset({"a", "b"}))
    b = ParticipantProfile(tag_ids=frozenset({"a", "b"}))
    assert compute_match_score(a, b, weights) == 0.0


def test_negative_weight_raises_value_error():
    with pytest.raises(ValueError, match="non-negative"):
        ScoringWeights(tag_weight=-0.1)


def test_nan_weight_raises_value_error():
    with pytest.raises(ValueError, match="finite"):
        ScoringWeights(tag_weight=math.nan)


def test_inf_weight_raises_value_error():
    with pytest.raises(ValueError, match="finite"):
        ScoringWeights(tag_weight=math.inf)


def test_deterministic_same_inputs_same_output():
    weights = ScoringWeights(tag_weight=1.0)
    a = ParticipantProfile(tag_ids=frozenset({"a", "b", "c"}))
    b = ParticipantProfile(tag_ids=frozenset({"b", "c", "d"}))

    first = compute_match_score(a, b, weights)
    second = compute_match_score(a, b, weights)

    assert first == second
