"""Disconnect hold, 4-minute split, and the one 6-minute recovery date."""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.events.live_matching import reconcile_live_round
from apps.events.models import Event, Pair, PairStatus, Participant, Round
from apps.events.orchestrator import connection_channel_key
from apps.events.presence import _presence_key


@pytest.fixture
def live_event(db):
    now = timezone.now()
    event = Event.objects.create(
        name="Recovery",
        start_time=now - timedelta(minutes=2),
        num_rounds=2,
        round_duration=timedelta(minutes=6),
        male_date_limit=4,
        female_date_limit=8,
    )
    round_obj = Round.objects.create(
        event=event,
        number=1,
        starts_at=now - timedelta(minutes=1),
        ends_at=now + timedelta(minutes=5),
    )
    cache.clear()
    return event, round_obj, now


def _person(event, name, gender):
    return Participant.objects.create(
        event=event,
        display_name=name,
        join_token_hash="!",
        gender=gender,
        want_to_date="Any",
        country="GB",
        city="London",
    )


def _ready(*participants, now=None):
    touched = (now or timezone.now()).timestamp()
    for participant in participants:
        channel = f"live-{participant.pk}"
        cache.set(connection_channel_key(participant.pk), channel, 300)
        cache.set(
            _presence_key(participant.pk),
            {"channel": channel, "ready": True, "touched": touched},
            45,
        )


def _connected_pair(event, round_obj, male, female, *, talk_started_at):
    return Pair.objects.create(
        event=event,
        round=round_obj,
        participant_a=male,
        participant_b=female,
        room_id=f"date-{male.pk}",
        status=PairStatus.ACTIVE,
        live_allocated=True,
        connection_type_a="direct",
        connection_type_b="relay",
        media_received_a=True,
        media_received_b=True,
        talk_started_at=talk_started_at,
    )


@pytest.mark.django_db(transaction=True)
def test_short_absence_holds_the_date_and_names_the_partner(live_event):
    event, round_obj, now = live_event
    male = _person(event, "Alex", "Male")
    female = _person(event, "Mina", "Female")
    pair = _connected_pair(event, round_obj, male, female, talk_started_at=now - timedelta(minutes=1))
    _ready(female, now=now)

    held = reconcile_live_round(event, round_obj, now=now)
    pair.refresh_from_db()
    assert pair.status == PairStatus.ACTIVE
    assert held["new_pairs"] == []
    assert held["holding"][str(female.pk)]["name"] == "Alex"
    assert held["holding"][str(female.pk)]["seconds_left"] == 60


@pytest.mark.django_db(transaction=True)
def test_short_talk_opens_one_six_minute_recovery_date(live_event):
    event, round_obj, now = live_event
    absent = _person(event, "Alex", "Male")
    staying = _person(event, "Mina", "Female")
    replacement = _person(event, "Sam", "Male")
    pair = _connected_pair(
        event, round_obj, absent, staying, talk_started_at=now - timedelta(minutes=3)
    )
    _ready(staying, replacement, now=now)

    reconcile_live_round(event, round_obj, now=now)
    later = now + timedelta(seconds=62)
    _ready(staying, replacement, now=later)
    opened = reconcile_live_round(event, round_obj, now=later)
    pair.refresh_from_db()
    assert pair.status == PairStatus.CANCELLED
    assert len(opened["new_pairs"]) == 1
    recovery = opened["new_pairs"][0]
    assert recovery.recovery is True
    assert recovery.date_ends_at - (now + timedelta(seconds=62)) == timedelta(minutes=6)
    assert {recovery.participant_a_id, recovery.participant_b_id} == {staying.pk, replacement.pk}


@pytest.mark.django_db(transaction=True)
def test_long_talk_waits_for_the_next_round(live_event):
    event, round_obj, now = live_event
    absent = _person(event, "Alex", "Male")
    staying = _person(event, "Mina", "Female")
    replacement = _person(event, "Sam", "Male")
    pair = _connected_pair(
        event, round_obj, absent, staying, talk_started_at=now - timedelta(minutes=5)
    )
    _ready(staying, replacement, now=now)
    Round.objects.create(
        event=event,
        number=2,
        starts_at=round_obj.ends_at + timedelta(minutes=2),
        ends_at=round_obj.ends_at + timedelta(minutes=8),
    )

    reconcile_live_round(event, round_obj, now=now)
    later = now + timedelta(seconds=62)
    _ready(staying, replacement, now=later)
    opened = reconcile_live_round(event, round_obj, now=later)
    pair.refresh_from_db()
    assert pair.status == PairStatus.CANCELLED
    assert opened["new_pairs"] == []
    assert opened["waiting"][str(staying.pk)] == "waiting_next_round"


@pytest.mark.django_db(transaction=True)
def test_finished_recovery_does_not_join_the_current_round(live_event):
    event, round_obj, now = live_event
    male = _person(event, "Alex", "Male")
    partner = _person(event, "Mina", "Female")
    other = _person(event, "Noor", "Female")
    recovery = Pair.objects.create(
        event=event,
        round=round_obj,
        participant_a=male,
        participant_b=partner,
        room_id="recovery-done",
        status=PairStatus.ACTIVE,
        live_allocated=True,
        recovery=True,
        connection_type_a="direct",
        connection_type_b="relay",
        media_received_a=True,
        media_received_b=True,
        date_ends_at=now - timedelta(seconds=1),
    )
    Round.objects.create(
        event=event,
        number=2,
        starts_at=now + timedelta(minutes=3),
        ends_at=now + timedelta(minutes=9),
    )
    _ready(male, other, now=now)

    result = reconcile_live_round(event, round_obj, now=now)
    recovery.refresh_from_db()
    assert recovery.status == PairStatus.COMPLETED
    assert result["new_pairs"] == []
    assert result["waiting"][str(male.pk)] == "waiting_next_round"


@pytest.mark.django_db(transaction=True)
def test_recovery_date_is_not_cut_by_the_round_clock(live_event):
    event, round_obj, now = live_event
    male = _person(event, "Alex", "Male")
    female = _person(event, "Mina", "Female")
    pair = _connected_pair(event, round_obj, male, female, talk_started_at=now - timedelta(minutes=1))
    pair.recovery = True
    pair.date_ends_at = now + timedelta(minutes=4)
    pair.save(update_fields=["recovery", "date_ends_at"])
    round_obj.ends_at = now - timedelta(seconds=1)
    round_obj.save(update_fields=["ends_at"])

    reconcile_live_round(event, round_obj, now=now)
    pair.refresh_from_db()
    assert pair.status == PairStatus.ACTIVE
