from datetime import timedelta
from uuid import UUID

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.events.models import Event, Pair, Participant, Round


def create_event(name: str) -> Event:
    return Event.objects.create(
        name=name,
        num_rounds=3,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(seconds=30),
        start_time=timezone.now(),
    )


def create_round(event: Event, number: int) -> Round:
    starts_at = event.start_time + ((number - 1) * timedelta(minutes=6))
    return Round.objects.create(
        event=event,
        number=number,
        starts_at=starts_at,
        ends_at=starts_at + event.round_duration,
    )


def create_participant(event: Event, number: int, name: str) -> Participant:
    return Participant.objects.create(
        id=UUID(int=number),
        event=event,
        display_name=name,
        join_token_hash="!",
    )


def create_pair(
    round_: Round,
    participant_a: Participant,
    participant_b: Participant,
    room_id: str,
) -> Pair:
    return Pair.objects.create(
        round=round_,
        participant_a=participant_a,
        participant_b=participant_b,
        room_id=room_id,
    )


@pytest.mark.django_db(transaction=True)
def test_valid_pair_succeeds_and_uses_canonical_ordering():
    event = create_event("Event")
    round_ = create_round(event, 1)
    participant_a = create_participant(event, 1, "A")
    participant_b = create_participant(event, 2, "B")

    pair = create_pair(round_, participant_b, participant_a, "room-valid")

    pair.refresh_from_db()
    assert pair.event == event
    assert pair.participant_a == participant_a
    assert pair.participant_b == participant_b


@pytest.mark.django_db(transaction=True)
def test_exact_pair_cannot_repeat_in_same_event():
    event = create_event("Event")
    round_1 = create_round(event, 1)
    round_2 = create_round(event, 2)
    participant_a = create_participant(event, 1, "A")
    participant_b = create_participant(event, 2, "B")
    create_pair(round_1, participant_a, participant_b, "room-first")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            create_pair(round_2, participant_a, participant_b, "room-duplicate")


@pytest.mark.django_db(transaction=True)
def test_reverse_order_cannot_repeat_pair_in_same_event():
    event = create_event("Event")
    round_1 = create_round(event, 1)
    round_2 = create_round(event, 2)
    participant_a = create_participant(event, 1, "A")
    participant_b = create_participant(event, 2, "B")
    create_pair(round_1, participant_a, participant_b, "room-first")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            create_pair(round_2, participant_b, participant_a, "room-reversed")


@pytest.mark.django_db(transaction=True)
def test_participant_cannot_be_participant_a_twice_in_round():
    event = create_event("Event")
    round_ = create_round(event, 1)
    participant_a = create_participant(event, 1, "A")
    participant_b = create_participant(event, 2, "B")
    participant_c = create_participant(event, 3, "C")
    create_pair(round_, participant_a, participant_b, "room-first")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            create_pair(round_, participant_a, participant_c, "room-conflict")


@pytest.mark.django_db(transaction=True)
def test_participant_cannot_cross_pair_columns_in_round():
    event = create_event("Event")
    round_ = create_round(event, 1)
    participant_a = create_participant(event, 1, "A")
    participant_b = create_participant(event, 2, "B")
    participant_c = create_participant(event, 3, "C")
    create_pair(round_, participant_a, participant_b, "room-first")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            create_pair(round_, participant_b, participant_c, "room-conflict")


@pytest.mark.django_db(transaction=True)
def test_participant_can_pair_in_different_rounds():
    event = create_event("Event")
    round_1 = create_round(event, 1)
    round_2 = create_round(event, 2)
    participant_a = create_participant(event, 1, "A")
    participant_b = create_participant(event, 2, "B")
    participant_c = create_participant(event, 3, "C")

    create_pair(round_1, participant_a, participant_b, "room-first")
    create_pair(round_2, participant_a, participant_c, "room-second")

    assert Pair.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_same_named_pair_is_allowed_in_different_events():
    first_event = create_event("First event")
    second_event = create_event("Second event")
    first_round = create_round(first_event, 1)
    second_round = create_round(second_event, 1)
    first_a = create_participant(first_event, 1, "A")
    first_b = create_participant(first_event, 2, "B")
    second_a = create_participant(second_event, 3, "A")
    second_b = create_participant(second_event, 4, "B")

    create_pair(first_round, first_a, first_b, "room-first")
    create_pair(second_round, second_a, second_b, "room-second")

    assert Pair.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_odd_participant_can_remain_unmatched():
    event = create_event("Event")
    round_ = create_round(event, 1)
    participants = [
        create_participant(event, number, name)
        for number, name in enumerate(("A", "B", "C", "D", "E"), start=1)
    ]

    create_pair(round_, participants[0], participants[1], "room-first")
    create_pair(round_, participants[2], participants[3], "room-second")

    assert Pair.objects.filter(round=round_).count() == 2
    assert (
        not Pair.objects.filter(participant_a=participants[4]).exists()
        and not Pair.objects.filter(participant_b=participants[4]).exists()
    )
