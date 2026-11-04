from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.events.auth import scheduled_event_end
from apps.events.matching import (
    MatchInput,
    MatchParticipant,
    generate_schedule,
    validate_schedule,
)
from apps.events.models import Event
from apps.events.scheduler import calculate_full_schedule
from apps.events.scoring import ParticipantProfile, ScoringWeights


def _participant(number: int, gender: str, preference: str, limit: int):
    return MatchParticipant(
        pid=str(uuid.UUID(int=number)),
        profile=ParticipantProfile(tag_ids=frozenset()),
        gender=gender,
        want_to_date=preference,
        max_dates=limit,
    )


def _input(participants, rounds: int) -> MatchInput:
    return MatchInput(
        participants=tuple(participants),
        num_rounds=rounds,
        weights=ScoringWeights(),
    )


@pytest.mark.django_db
def test_new_event_defaults_to_five_seven_minute_dates_and_two_minute_breaks():
    event = Event.objects.create(name="Defaults", start_time=timezone.now())

    assert event.num_rounds == 5
    assert event.round_duration == timedelta(minutes=7)
    assert event.break_duration == timedelta(minutes=2)
    assert event.male_date_limit == 5
    assert event.female_date_limit == 7
    assert event.other_date_limit == 5


@pytest.mark.django_db
def test_intro_video_duration_precedes_first_preconnect_and_round():
    start = timezone.now()
    event = Event.objects.create(
        name="Intro",
        start_time=start,
        num_rounds=2,
        intro_video_url="https://cdn.example.com/intro.mp4",
        intro_video_duration_seconds=75,
    )

    [first, second] = calculate_full_schedule(event)

    assert first.preconnect_starts_at == start + timedelta(seconds=75)
    assert first.starts_at == start + timedelta(seconds=95)
    assert first.ends_at == first.starts_at + timedelta(minutes=7)
    assert second.starts_at - first.ends_at == timedelta(minutes=2)
    assert scheduled_event_end(event) == second.ends_at


def test_matching_respects_mutual_preferences_and_participant_caps():
    match_input = _input(
        [
            _participant(1, "Male", "Female", 1),
            _participant(2, "Female", "Male", 1),
            _participant(3, "Female", "Female", 1),
        ],
        rounds=2,
    )

    schedule = generate_schedule(match_input)

    pairings = [pair for rnd in schedule.rounds for pair in rnd.pairs]
    assert len(pairings) == 1
    assert {pairings[0].pid_a, pairings[0].pid_b} == {
        match_input.participants[0].pid,
        match_input.participants[1].pid,
    }
    assert validate_schedule(
        schedule,
        frozenset(p.pid for p in match_input.participants),
        num_rounds=2,
        match_input=match_input,
    ) == []


def test_matching_supports_mutual_preferences_across_all_gender_categories():
    participants = [
        _participant(11, "Female", "Any", 3),
        _participant(12, "Female", "Any", 3),
        _participant(13, "Male", "Any", 3),
        _participant(14, "Male", "Any", 3),
        _participant(15, "Other/Prefer Not to Say", "Any", 3),
        _participant(16, "Other/Prefer Not to Say", "Any", 3),
    ]
    match_input = _input(participants, rounds=3)

    schedule = generate_schedule(match_input)

    assert all(len(rnd.pairs) == 3 for rnd in schedule.rounds)
    assert validate_schedule(
        schedule,
        frozenset(p.pid for p in participants),
        num_rounds=3,
        match_input=match_input,
    ) == []


@pytest.mark.parametrize("women_count", [250, 300])
def test_imbalanced_event_simulation_respects_caps_and_rotates_men(women_count):
    men_count = 700
    participants = [
        _participant(i + 1, "Female", "Male", 7) for i in range(women_count)
    ] + [
        _participant(women_count + i + 1, "Male", "Female", 5)
        for i in range(men_count)
    ]
    match_input = _input(participants, rounds=7)

    schedule = generate_schedule(match_input)

    paired_counts = {p.pid: 0 for p in participants}
    all_pairs = set()
    for rnd in schedule.rounds:
        assert len(rnd.pairs) == women_count
        for pair in rnd.pairs:
            assert pair.pid_a < pair.pid_b
            key = (pair.pid_a, pair.pid_b)
            assert key not in all_pairs
            all_pairs.add(key)
            paired_counts[pair.pid_a] += 1
            paired_counts[pair.pid_b] += 1

    women_counts = [paired_counts[p.pid] for p in participants[:women_count]]
    men_counts = [paired_counts[p.pid] for p in participants[women_count:]]
    assert set(women_counts) == {7}
    assert max(men_counts) <= 5
    assert max(men_counts) - min(men_counts) <= 1
    assert validate_schedule(
        schedule,
        frozenset(p.pid for p in participants),
        num_rounds=7,
        match_input=match_input,
    ) == []


def test_unmatched_participant_ids_are_reported_for_partial_rounds():
    match_input = _input(
        [
            _participant(1, "Female", "Male", 2),
            _participant(2, "Male", "Female", 2),
            _participant(3, "Male", "Female", 2),
            _participant(4, "Male", "Female", 2),
        ],
        rounds=1,
    )

    schedule = generate_schedule(match_input)

    assert len(schedule.rounds[0].pairs) == 1
    assert len(schedule.rounds[0].all_unmatched_pids) == 2
