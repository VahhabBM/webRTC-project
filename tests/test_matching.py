import math
import uuid

import pytest

from apps.events.matching import (
    GeneratedSchedule,
    MatchingImpossibleError,
    MatchInput,
    MatchParticipant,
    SchedulePair,
    ScheduleRound,
    _canon,
    generate_schedule,
    validate_schedule,
)


def _make_pid(n: int) -> str:
    """Return a deterministic UUID string for participant index n."""

    from uuid import UUID

    return str(UUID(int=n))


def _make_participant(n: int, tag_ids: frozenset[str]) -> MatchParticipant:
    from apps.events.scoring import ParticipantProfile

    return MatchParticipant(
        pid=_make_pid(n), profile=ParticipantProfile(tag_ids=tag_ids)
    )


def _make_input(
    n_participants: int,
    num_rounds: int,
    tag_weight: float = 1.0,
    tag_fn=None,
) -> MatchInput:
    from apps.events.scoring import ScoringWeights

    tag_fn = tag_fn or (lambda i: frozenset())
    parts = tuple(
        sorted(
            [_make_participant(i, tag_fn(i)) for i in range(1, n_participants + 1)],
            key=lambda p: p.pid,
        )
    )
    return MatchInput(
        participants=parts,
        num_rounds=num_rounds,
        weights=ScoringWeights(tag_weight=tag_weight),
    )


def test_validator_empty_schedule_wrong_round_count():
    schedule = GeneratedSchedule(rounds=())
    pids = frozenset({_make_pid(1), _make_pid(2)})
    violations = validate_schedule(schedule, pids, num_rounds=1)
    kinds = {v.kind for v in violations}
    assert "WRONG_ROUND_COUNT" in kinds


def test_validator_self_pair_detected():
    pid = _make_pid(1)
    pair = SchedulePair(pid_a=pid, pid_b=pid, score=0.0)
    rnd = ScheduleRound(number=1, pairs=(pair,), unmatched_pid=None)
    violations = validate_schedule(
        GeneratedSchedule(rounds=(rnd,)),
        frozenset({pid}),
        num_rounds=1,
    )
    assert any(v.kind == "SELF_PAIR" for v in violations)


def test_validator_repeated_partner_detected():
    a, b = _make_pid(1), _make_pid(2)
    ca, cb = _canon(a, b)
    pair = SchedulePair(pid_a=ca, pid_b=cb, score=0.0)
    r1 = ScheduleRound(number=1, pairs=(pair,), unmatched_pid=None)
    r2 = ScheduleRound(number=2, pairs=(pair,), unmatched_pid=None)
    violations = validate_schedule(
        GeneratedSchedule(rounds=(r1, r2)),
        frozenset({a, b}),
        num_rounds=2,
    )
    assert any(v.kind == "REPEATED_PARTNER" for v in violations)


def test_validator_wrong_pair_count_even():
    a, b, c, d = [_make_pid(i) for i in range(1, 5)]
    ca, cb = _canon(a, b)
    ab = SchedulePair(pid_a=ca, pid_b=cb, score=0.0)
    rnd = ScheduleRound(number=1, pairs=(ab,), unmatched_pid=None)
    violations = validate_schedule(
        GeneratedSchedule(rounds=(rnd,)),
        frozenset({a, b, c, d}),
        num_rounds=1,
    )
    assert any(v.kind == "WRONG_PAIR_COUNT" for v in violations)


def test_validator_unmatched_twice():
    a, b, c = _make_pid(1), _make_pid(2), _make_pid(3)
    ca, cb = _canon(a, b)
    ab = SchedulePair(pid_a=ca, pid_b=cb, score=0.0)
    r1 = ScheduleRound(number=1, pairs=(ab,), unmatched_pid=c)
    r2 = ScheduleRound(number=2, pairs=(ab,), unmatched_pid=c)
    violations = validate_schedule(
        GeneratedSchedule(rounds=(r1, r2)),
        frozenset({a, b, c}),
        num_rounds=2,
    )
    assert any(v.kind == "UNMATCHED_TWICE" for v in violations)


def test_validator_clean_even_4_participants_2_rounds():
    """A hand-crafted valid schedule must produce zero violations."""

    a, b, c, d = sorted([_make_pid(i) for i in range(1, 5)], key=lambda p: uuid.UUID(p))
    pair_ab = SchedulePair(pid_a=a, pid_b=b, score=0.0)
    pair_cd = SchedulePair(pid_a=c, pid_b=d, score=0.0)
    pair_ac = SchedulePair(pid_a=a, pid_b=c, score=0.0)
    pair_bd = SchedulePair(pid_a=b, pid_b=d, score=0.0)
    schedule = GeneratedSchedule(
        rounds=(
            ScheduleRound(number=1, pairs=(pair_ab, pair_cd), unmatched_pid=None),
            ScheduleRound(number=2, pairs=(pair_ac, pair_bd), unmatched_pid=None),
        )
    )
    assert validate_schedule(schedule, frozenset({a, b, c, d}), num_rounds=2) == []


def test_generate_even_4_participants_3_rounds():
    mi = _make_input(4, num_rounds=3)
    schedule = generate_schedule(mi)
    pids = frozenset(p.pid for p in mi.participants)
    violations = validate_schedule(schedule, pids, num_rounds=3)
    assert violations == []
    assert len(schedule.rounds) == 3
    for rnd in schedule.rounds:
        assert len(rnd.pairs) == 2
        assert rnd.unmatched_pid is None


def test_generate_odd_7_participants_6_rounds():
    mi = _make_input(7, num_rounds=6)
    schedule = generate_schedule(mi)
    pids = frozenset(p.pid for p in mi.participants)
    assert validate_schedule(schedule, pids, num_rounds=6) == []
    for rnd in schedule.rounds:
        assert rnd.unmatched_pid is not None
        assert len(rnd.pairs) == 3
    unmatched = [rnd.unmatched_pid for rnd in schedule.rounds]
    assert len(unmatched) == len(set(unmatched))


def test_generate_even_8_participants_6_rounds():
    mi = _make_input(8, num_rounds=6)
    schedule = generate_schedule(mi)
    pids = frozenset(p.pid for p in mi.participants)
    assert validate_schedule(schedule, pids, num_rounds=6) == []
    for rnd in schedule.rounds:
        assert len(rnd.pairs) == 4
        assert rnd.unmatched_pid is None
    all_pair_keys = [
        _canon(sp.pid_a, sp.pid_b) for rnd in schedule.rounds for sp in rnd.pairs
    ]
    assert len(all_pair_keys) == len(set(all_pair_keys))


def test_generate_impossible_n_equals_r():
    """N = R is structurally infeasible; must raise before attempting greedy."""

    with pytest.raises(MatchingImpossibleError):
        generate_schedule(_make_input(6, num_rounds=6))


def test_generate_impossible_n_less_than_r():
    with pytest.raises(MatchingImpossibleError):
        generate_schedule(_make_input(5, num_rounds=6))


def test_generate_impossible_single_participant():
    with pytest.raises(MatchingImpossibleError):
        generate_schedule(_make_input(1, num_rounds=1))


def test_generate_impossible_zero_participants():
    with pytest.raises(MatchingImpossibleError):
        generate_schedule(_make_input(0, num_rounds=1))


def test_generate_determinism():
    """Two calls with identical MatchInput must produce byte-identical schedules."""

    mi = _make_input(12, num_rounds=3)
    s1 = generate_schedule(mi)
    s2 = generate_schedule(mi)
    assert s1 == s2


def test_all_scores_in_range():
    mi = _make_input(12, num_rounds=6)
    schedule = generate_schedule(mi)
    for rnd in schedule.rounds:
        for sp in rnd.pairs:
            assert 0.0 <= sp.score <= 1.0
            assert not math.isnan(sp.score)
            assert not math.isinf(sp.score)


def test_local_improvement_accepts_improving_swap():
    from uuid import UUID

    from apps.events.scoring import ParticipantProfile, ScoringWeights

    pid1 = str(UUID(int=1))
    pid2 = str(UUID(int=2))
    pid3 = str(UUID(int=3))
    pid4 = str(UUID(int=4))

    participants = tuple(
        sorted(
            [
                MatchParticipant(
                    pid=pid1,
                    profile=ParticipantProfile(tag_ids=frozenset({"a", "b", "c"})),
                ),
                MatchParticipant(
                    pid=pid2,
                    profile=ParticipantProfile(tag_ids=frozenset({"d", "e", "f"})),
                ),
                MatchParticipant(
                    pid=pid3,
                    profile=ParticipantProfile(tag_ids=frozenset({"a", "b", "x"})),
                ),
                MatchParticipant(
                    pid=pid4,
                    profile=ParticipantProfile(tag_ids=frozenset({"a", "b", "x"})),
                ),
            ],
            key=lambda p: p.pid,
        )
    )

    mi = MatchInput(
        participants=participants,
        num_rounds=1,
        weights=ScoringWeights(tag_weight=1.0),
    )

    schedule = generate_schedule(mi)

    assert len(schedule.rounds) == 1
    rnd = schedule.rounds[0]
    assert len(rnd.pairs) == 2

    pair_keys = {_canon(sp.pid_a, sp.pid_b) for sp in rnd.pairs}
    p3p4_key = _canon(pid3, pid4)
    assert p3p4_key in pair_keys, (
        f"Expected (P3,P4) pair after local improvement, but found pairs: {pair_keys}"
    )

    total = sum(sp.score for sp in rnd.pairs)
    assert total == pytest.approx(1.0)

    pids = frozenset(p.pid for p in mi.participants)
    assert validate_schedule(schedule, pids, num_rounds=1) == []


def _db_event(name="T", num_rounds=6):
    from datetime import timedelta

    from django.utils import timezone

    from apps.events.models import Event

    return Event.objects.create(
        name=name,
        num_rounds=num_rounds,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(seconds=30),
        start_time=timezone.now(),
    )


def _db_participants(event, n):
    from uuid import UUID

    from apps.events.models import Participant

    objs = [
        Participant(
            id=UUID(int=i + 1),
            event=event,
            display_name=f"P{i + 1:04d}",
            join_token_hash="!",
        )
        for i in range(n)
    ]
    Participant.objects.bulk_create(objs)
    return objs


@pytest.mark.django_db(transaction=True)
def test_persist_even_8_creates_correct_counts():
    event = _db_event(num_rounds=6)
    _db_participants(event, 8)
    from django.core.management import call_command

    call_command("generate_schedule", str(event.pk))
    from apps.events.models import Pair, Round

    assert Round.objects.filter(event=event).count() == 6
    assert Pair.objects.filter(event=event).count() == 24


@pytest.mark.django_db(transaction=True)
def test_persist_odd_9_creates_correct_counts():
    event = _db_event(num_rounds=6)
    _db_participants(event, 9)
    from django.core.management import call_command

    call_command("generate_schedule", str(event.pk))
    from apps.events.models import Pair, Round

    assert Round.objects.filter(event=event).count() == 6
    assert Pair.objects.filter(event=event).count() == 24


@pytest.mark.django_db(transaction=True)
def test_rerun_replaces_not_appends():
    event = _db_event(num_rounds=6)
    _db_participants(event, 8)
    from django.core.management import call_command

    from apps.events.models import Pair, Round

    call_command("generate_schedule", str(event.pk))
    first_round_pks = set(
        Round.objects.filter(event=event).values_list("pk", flat=True)
    )
    call_command("generate_schedule", str(event.pk))
    assert Round.objects.filter(event=event).count() == 6
    assert Pair.objects.filter(event=event).count() == 24
    second_round_pks = set(
        Round.objects.filter(event=event).values_list("pk", flat=True)
    )
    assert first_round_pks != second_round_pks


@pytest.mark.django_db(transaction=True)
def test_rerun_atomicity_preserves_state_on_failure(monkeypatch):
    event = _db_event(num_rounds=6)
    _db_participants(event, 8)
    from django.core.management import call_command

    from apps.events.models import Pair, Round

    call_command("generate_schedule", str(event.pk))

    first_round_pks = set(
        Round.objects.filter(event=event).values_list("pk", flat=True)
    )
    first_pair_count = Pair.objects.filter(event=event).count()

    def failing_bulk_create(objs, **kwargs):
        raise RuntimeError("simulated bulk_create failure")

    monkeypatch.setattr(Pair.objects, "bulk_create", failing_bulk_create)

    with pytest.raises((RuntimeError, Exception)):
        call_command("generate_schedule", str(event.pk))

    assert Round.objects.filter(event=event).count() == 6
    assert Pair.objects.filter(event=event).count() == first_pair_count
    assert (
        set(Round.objects.filter(event=event).values_list("pk", flat=True))
        == first_round_pks
    )


@pytest.mark.django_db(transaction=True)
def test_no_repeated_partner_in_db():
    event = _db_event(num_rounds=6)
    _db_participants(event, 12)
    from django.core.management import call_command

    from apps.events.models import Pair

    call_command("generate_schedule", str(event.pk))
    all_pairs = list(
        Pair.objects.filter(event=event).values_list(
            "participant_a_id", "participant_b_id"
        )
    )
    assert len(all_pairs) == len(set(all_pairs))


@pytest.mark.django_db(transaction=True)
def test_no_participant_twice_per_round():
    event = _db_event(num_rounds=6)
    _db_participants(event, 10)
    from django.core.management import call_command

    from apps.events.models import Pair, Round

    call_command("generate_schedule", str(event.pk))
    for round_obj in Round.objects.filter(event=event):
        pids = []
        for a, b in Pair.objects.filter(round=round_obj).values_list(
            "participant_a_id", "participant_b_id"
        ):
            pids.extend([str(a), str(b)])
        assert len(pids) == len(set(pids)), (
            f"Participant appears twice in round {round_obj.number}"
        )


@pytest.mark.django_db(transaction=True)
def test_room_ids_are_globally_unique():
    event = _db_event(num_rounds=6)
    _db_participants(event, 10)
    from django.core.management import call_command

    from apps.events.models import Pair

    call_command("generate_schedule", str(event.pk))
    room_ids = list(Pair.objects.filter(event=event).values_list("room_id", flat=True))
    assert len(room_ids) == len(set(room_ids))


@pytest.mark.django_db(transaction=True)
def test_room_ids_are_deterministic_across_reruns():
    event = _db_event(num_rounds=6)
    _db_participants(event, 10)
    from django.core.management import call_command

    from apps.events.models import Pair

    call_command("generate_schedule", str(event.pk))
    first_room_ids = set(
        Pair.objects.filter(event=event).values_list("room_id", flat=True)
    )
    call_command("generate_schedule", str(event.pk))
    second_room_ids = set(
        Pair.objects.filter(event=event).values_list("room_id", flat=True)
    )
    assert first_room_ids == second_room_ids


@pytest.mark.django_db(transaction=True)
def test_dry_run_writes_nothing():
    event = _db_event(num_rounds=6)
    _db_participants(event, 8)
    from django.core.management import call_command

    from apps.events.models import Pair, Round

    call_command("generate_schedule", str(event.pk), dry_run=True)
    assert Round.objects.filter(event=event).count() == 0
    assert Pair.objects.filter(event=event).count() == 0


@pytest.mark.django_db(transaction=True)
def test_command_invalid_uuid_raises():
    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="Invalid UUID"):
        call_command("generate_schedule", "not-a-valid-uuid")


@pytest.mark.django_db(transaction=True)
def test_command_missing_event_raises():
    import uuid as uuid_mod

    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="does not exist"):
        call_command("generate_schedule", str(uuid_mod.uuid4()))


@pytest.mark.django_db(transaction=True)
def test_command_too_few_participants_raises():
    from django.core.management import call_command
    from django.core.management.base import CommandError

    event = _db_event(num_rounds=6)
    _db_participants(event, 1)
    with pytest.raises(CommandError):
        call_command("generate_schedule", str(event.pk))


@pytest.mark.django_db(transaction=True)
def test_900_participant_benchmark():
    """End-to-end benchmark using a realistic seed event.

    Design target:  schedule generation completes in under 10 seconds.
    CI hard limit:  the assertion below uses 30 seconds to tolerate slow
                    CI runners without false failures.  A result between
                    10 s and 30 s indicates a performance regression worth
                    investigating but does not block CI.
    """

    import time

    from django.core.management import call_command

    from apps.events.models import Event, Pair, Round

    call_command(
        "seed_event",
        participants=900,
        seed=42,
        num_rounds=6,
        event_name="Benchmark 900",
    )
    event = Event.objects.get(name__startswith="Benchmark 900")

    t0 = time.perf_counter()
    call_command("generate_schedule", str(event.pk))
    elapsed = time.perf_counter() - t0
    print(
        f"\n[benchmark] 900-participant schedule generated in {elapsed:.2f}s "
        "(design target <10s, CI limit <30s)"
    )

    assert Round.objects.filter(event=event).count() == 6
    assert Pair.objects.filter(event=event).count() == 2700

    all_pairs = list(
        Pair.objects.filter(event=event).values_list(
            "participant_a_id", "participant_b_id"
        )
    )
    assert len(all_pairs) == len(set(all_pairs)), "Repeated partner detected!"

    for round_obj in Round.objects.filter(event=event):
        pids_in_round = []
        for a, b in Pair.objects.filter(round=round_obj).values_list(
            "participant_a_id", "participant_b_id"
        ):
            pids_in_round.extend([a, b])
        assert len(pids_in_round) == len(set(pids_in_round)), (
            f"Participant appears twice in round {round_obj.number}"
        )

    assert elapsed < 30.0, (
        f"Schedule generation took {elapsed:.2f}s, exceeding the 30s CI limit. "
        "The design target is <10s."
    )
