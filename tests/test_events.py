from datetime import UTC, timedelta

import pytest
from django.contrib import admin
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.events.models import (
    Event,
    EventStatus,
    Pair,
    PairStatus,
    Participant,
    ParticipantStatus,
    ParticipantTag,
    Round,
    RoundStatus,
    Tag,
)


@pytest.fixture
def event(db):
    return Event.objects.create(
        name="Networking night",
        num_rounds=3,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(seconds=30),
        start_time=timezone.now(),
    )


@pytest.fixture
def participant(event):
    participant = Participant.objects.create(
        event=event,
        display_name="Alex",
        join_token_hash="!",
    )
    participant.set_join_token("secret-token")
    participant.save(update_fields=["join_token_hash"])
    return participant


@pytest.mark.django_db
def test_event_configuration_and_utc_datetime(event):
    assert event.num_rounds == 3
    assert event.round_duration == timedelta(minutes=5)
    assert event.break_duration == timedelta(seconds=30)
    assert timezone.is_aware(event.start_time)
    assert event.start_time.tzinfo == UTC


@pytest.mark.django_db
def test_status_fields_expose_only_defined_choices(event, participant):
    assert {value for value, _ in EventStatus.choices} == {
        event.status
        for event in [Event(status=value) for value, _ in EventStatus.choices]
    }
    assert participant.status in ParticipantStatus.values
    assert RoundStatus.ACTIVE in RoundStatus.values
    assert PairStatus.COMPLETED in PairStatus.values


@pytest.mark.django_db
def test_participant_token_is_hashed_and_verifiable(participant):
    assert participant.join_token_hash != "secret-token"
    assert participant.verify_join_token("secret-token")
    assert not participant.verify_join_token("wrong-token")
    assert "secret-token" not in participant.join_token_hash


@pytest.mark.django_db
def test_relationships_and_uniqueness(event, participant):
    tag = Tag.objects.create(name="Python")
    ParticipantTag.objects.create(participant=participant, tag=tag)
    assert list(participant.tags.all()) == [tag]
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ParticipantTag.objects.create(participant=participant, tag=tag)

    round_ = Round.objects.create(
        event=event,
        number=1,
        starts_at=event.start_time,
        ends_at=event.start_time + event.round_duration,
    )
    second = Participant.objects.create(
        event=event, display_name="Blair", join_token_hash="!"
    )
    pair = Pair.objects.create(
        round=round_,
        participant_a=participant,
        participant_b=second,
        room_id="room-1",
    )
    assert pair.round.event == event
    assert list(round_.pairs.all()) == [pair]


@pytest.mark.django_db
def test_round_and_pair_constraints(event, participant):
    starts = event.start_time
    Round.objects.create(
        event=event,
        number=1,
        starts_at=starts,
        ends_at=starts + event.round_duration,
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Round.objects.create(
                event=event,
                number=1,
                starts_at=starts,
                ends_at=starts + event.round_duration,
            )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Round.objects.create(
                event=event, number=2, starts_at=starts, ends_at=starts
            )


@pytest.mark.django_db
def test_deleting_event_cascades_all_event_data(event, participant):
    tag = Tag.objects.create(name="Design")
    ParticipantTag.objects.create(participant=participant, tag=tag)
    round_ = Round.objects.create(
        event=event,
        number=1,
        starts_at=event.start_time,
        ends_at=event.start_time + event.round_duration,
    )
    second = Participant.objects.create(
        event=event, display_name="Blair", join_token_hash="!"
    )
    Pair.objects.create(
        round=round_,
        participant_a=participant,
        participant_b=second,
        room_id="room-delete",
    )
    event.delete()
    assert not Event.objects.exists()
    assert not Participant.objects.exists()
    assert not Round.objects.exists()
    assert not Pair.objects.exists()
    assert not ParticipantTag.objects.exists()
    assert Tag.objects.filter(pk=tag.pk).exists()


@pytest.mark.django_db
def test_models_are_registered_in_admin():
    for model in (Event, Tag, Participant, ParticipantTag, Round, Pair):
        assert model in admin.site._registry
