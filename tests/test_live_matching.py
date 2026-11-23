from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.events.live_matching import reconcile_live_round
from apps.events.models import Event, Pair, PairStatus, Participant, Round, Tag
from apps.events.orchestrator import connection_channel_key
from apps.events.presence import _presence_key


@pytest.fixture
def live_event(db):
    now = timezone.now()
    event = Event.objects.create(
        name="Live allocation",
        start_time=now - timedelta(minutes=2),
        num_rounds=1,
        round_duration=timedelta(minutes=5),
        male_date_limit=4,
        female_date_limit=8,
    )
    round_obj = Round.objects.create(
        event=event,
        number=1,
        starts_at=now - timedelta(minutes=1),
        ends_at=now + timedelta(minutes=4),
    )
    cache.clear()
    return event, round_obj, now


def _person(event, name, gender, *, country="US", city="Austin", preference="Any"):
    return Participant.objects.create(
        event=event,
        display_name=name,
        join_token_hash="!",
        gender=gender,
        want_to_date=preference,
        country=country,
        city=city,
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


@pytest.mark.django_db(transaction=True)
def test_only_fresh_ready_presence_is_allocated(live_event):
    event, round_obj, now = live_event
    a = _person(event, "A", "Male")
    b = _person(event, "B", "Female")
    c = _person(event, "C", "Male")
    _ready(a, b, now=now)
    cache.set(connection_channel_key(c.pk), "c-channel", 300)
    cache.set(
        _presence_key(c.pk),
        {"channel": "c-channel", "ready": False, "touched": now.timestamp()},
        45,
    )
    stale = _person(event, "D", "Female")
    channel = "stale-channel"
    cache.set(connection_channel_key(stale.pk), channel, 300)
    cache.set(
        _presence_key(stale.pk),
        {
            "channel": channel,
            "ready": True,
            "touched": (now - timedelta(seconds=60)).timestamp(),
        },
        45,
    )

    result = reconcile_live_round(event, round_obj, now=now)
    assert result["online_ids"] == {str(a.pk), str(b.pk)}
    assert len(result["new_pairs"]) == 1
    assert {
        result["new_pairs"][0].participant_a_id,
        result["new_pairs"][0].participant_b_id,
    } == {a.pk, b.pk}


@pytest.mark.django_db(transaction=True)
def test_matching_prefers_same_city_and_is_idempotent(live_event):
    event, round_obj, now = live_event
    m1 = _person(event, "M1", "Male", city="Austin")
    m2 = _person(event, "M2", "Male", city="Seattle")
    f1 = _person(event, "F1", "Female", city="Austin")
    f2 = _person(event, "F2", "Female", city="Seattle")
    _ready(m1, m2, f1, f2, now=now)
    tag = Tag.objects.create(name="music")
    m1.tags.add(tag)
    f1.tags.add(tag)

    first = reconcile_live_round(event, round_obj, now=now)
    assert len(first["new_pairs"]) == 2
    pairings = {
        frozenset((str(pair.participant_a_id), str(pair.participant_b_id)))
        for pair in first["new_pairs"]
    }
    assert frozenset((str(m1.pk), str(f1.pk))) in pairings
    assert frozenset((str(m2.pk), str(f2.pk))) in pairings
    second = reconcile_live_round(event, round_obj, now=now + timedelta(seconds=1))
    assert second["new_pairs"] == []
    assert Pair.objects.filter(round=round_obj, live_allocated=True).count() == 2


@pytest.mark.django_db(transaction=True)
def test_fairness_prefers_participant_with_fewer_successful_dates(live_event):
    event, round_obj, now = live_event
    experienced = _person(event, "Experienced", "Male")
    newer = _person(event, "Newer", "Male")
    available = _person(event, "Available", "Female")
    past_partner = _person(event, "Past", "Female")
    prior_round = Round.objects.create(
        event=event,
        number=2,
        starts_at=now - timedelta(hours=1),
        ends_at=now - timedelta(minutes=30),
    )
    Pair.objects.create(
        event=event,
        round=prior_round,
        participant_a=experienced,
        participant_b=past_partner,
        room_id="fairness-history",
        status=PairStatus.CANCELLED,
        connection_type_a="direct",
        connection_type_b="relay",
        media_received_a=True,
        media_received_b=True,
    )
    _ready(experienced, newer, available, now=now)

    result = reconcile_live_round(event, round_obj, now=now)
    assert len(result["new_pairs"]) == 1
    pair = result["new_pairs"][0]
    assert {pair.participant_a_id, pair.participant_b_id} == {newer.pk, available.pk}


@pytest.mark.django_db(transaction=True)
def test_caps_fairness_and_failed_attempt_does_not_consume_success_quota(live_event):
    event, round_obj, now = live_event
    event.male_date_limit = 2
    event.female_date_limit = 2
    event.save(update_fields=["male_date_limit", "female_date_limit"])
    used_male = _person(event, "Used", "Male")
    fresh_male = _person(event, "Fresh", "Male")
    female = _person(event, "Woman", "Female")
    old_partner = _person(event, "Old partner", "Female")
    prior_round = Round.objects.create(
        event=event,
        number=2,
        starts_at=now - timedelta(hours=1),
        ends_at=now - timedelta(minutes=30),
    )
    Pair.objects.create(
        event=event,
        round=prior_round,
        participant_a=used_male,
        participant_b=old_partner,
        room_id="successful-old-call",
        status=PairStatus.CANCELLED,
        connection_type_a="direct",
        connection_type_b="relay",
        media_received_a=True,
        media_received_b=True,
    )
    failed = Pair.objects.create(
        event=event,
        round=round_obj,
        participant_a=used_male,
        participant_b=female,
        room_id="failed-attempt",
        status=PairStatus.PENDING,
        live_allocated=True,
        created_at=now - timedelta(seconds=61),
    )
    failed.created_at = now - timedelta(seconds=61)
    failed.save(update_fields=["created_at"])
    _ready(fresh_male, female, now=now)

    result = reconcile_live_round(event, round_obj, now=now)
    failed.refresh_from_db()
    assert failed.status == PairStatus.CANCELLED
    assert result["cancelled_participant_ids"] == {
        str(used_male.pk), str(female.pk)
    }
    assert len(result["new_pairs"]) == 1
    assert {
        result["new_pairs"][0].participant_a_id,
        result["new_pairs"][0].participant_b_id,
    } == {fresh_male.pk, female.pk}


@pytest.mark.django_db(transaction=True)
def test_ice_connectivity_without_both_media_receipts_is_not_a_success(live_event):
    event, round_obj, now = live_event
    event.male_date_limit = 1
    event.female_date_limit = 1
    event.save(update_fields=["male_date_limit", "female_date_limit"])
    failed_male = _person(event, "ICE only male", "Male")
    available_female = _person(event, "Available female", "Female")
    replacement_male = _person(event, "Replacement male", "Male")
    ice_only = Pair.objects.create(
        event=event,
        round=round_obj,
        participant_a=failed_male,
        participant_b=available_female,
        room_id="ice-without-media",
        status=PairStatus.PENDING,
        live_allocated=True,
        connection_type_a="direct",
        connection_type_b="relay",
        media_received_a=False,
        media_received_b=False,
    )
    ice_only.created_at = now - timedelta(seconds=61)
    ice_only.save(update_fields=["created_at"])
    _ready(available_female, replacement_male, now=now)

    result = reconcile_live_round(event, round_obj, now=now)
    ice_only.refresh_from_db()
    assert ice_only.status == PairStatus.CANCELLED
    assert len(result["new_pairs"]) == 1
    assert {
        result["new_pairs"][0].participant_a_id,
        result["new_pairs"][0].participant_b_id,
    } == {available_female.pk, replacement_male.pk}


@pytest.mark.django_db(transaction=True)
def test_reconnect_grace_preserves_then_cancels_and_requeues(live_event):
    event, round_obj, now = live_event
    male = _person(event, "M", "Male")
    female = _person(event, "F", "Female")
    _ready(male, female, now=now)
    created = reconcile_live_round(event, round_obj, now=now)["new_pairs"][0]

    cache.delete(_presence_key(female.pk))
    result = reconcile_live_round(event, round_obj, now=now + timedelta(seconds=1))
    assert result["cancelled_participant_ids"] == set()
    created.refresh_from_db()
    assert created.status == PairStatus.PENDING

    result = reconcile_live_round(event, round_obj, now=now + timedelta(seconds=17))
    created.refresh_from_db()
    assert created.status == PairStatus.CANCELLED
    assert result["cancelled_participant_ids"] == {str(male.pk), str(female.pk)}
    assert result["waiting"][str(male.pk)] == "partner_left"


@pytest.mark.django_db(transaction=True)
def test_connected_pairs_are_retained_and_expired_pairs_record_outcome(live_event):
    event, round_obj, now = live_event
    male = _person(event, "M", "Male")
    female = _person(event, "F", "Female")
    pair = Pair.objects.create(
        event=event,
        round=round_obj,
        participant_a=male,
        participant_b=female,
        room_id="connected",
        status=PairStatus.PENDING,
        live_allocated=True,
        connection_type_a="direct",
        connection_type_b="relay",
        media_received_a=True,
        media_received_b=True,
    )
    result = reconcile_live_round(event, round_obj, now=now)
    pair.refresh_from_db()
    assert pair.status == PairStatus.ACTIVE
    assert result["cancelled_participant_ids"] == set()

    round_obj.ends_at = now - timedelta(seconds=1)
    round_obj.save(update_fields=["ends_at"])
    reconcile_live_round(event, round_obj, now=now)
    pair.refresh_from_db()
    assert pair.status == PairStatus.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_established_call_absence_grace_releases_online_counterpart(live_event):
    event, round_obj, now = live_event
    absent_male = _person(event, "Absent male", "Male")
    present_female = _person(event, "Present female", "Female")
    replacement_male = _person(event, "Replacement male", "Male")
    pair = Pair.objects.create(
        event=event,
        round=round_obj,
        participant_a=absent_male,
        participant_b=present_female,
        room_id="established-but-peer-left",
        status=PairStatus.ACTIVE,
        live_allocated=True,
        connection_type_a="direct",
        connection_type_b="relay",
        media_received_a=True,
        media_received_b=True,
    )
    _ready(present_female, replacement_male, now=now)

    grace = reconcile_live_round(event, round_obj, now=now + timedelta(seconds=1))
    pair.refresh_from_db()
    assert pair.status == PairStatus.ACTIVE
    assert grace["cancelled_participant_ids"] == set()

    expired = reconcile_live_round(event, round_obj, now=now + timedelta(seconds=17))
    pair.refresh_from_db()
    assert pair.status == PairStatus.CANCELLED
    assert expired["cancelled_participant_ids"] == {
        str(absent_male.pk), str(present_female.pk)
    }
    assert len(expired["new_pairs"]) == 1
    assert {
        expired["new_pairs"][0].participant_a_id,
        expired["new_pairs"][0].participant_b_id,
    } == {present_female.pk, replacement_male.pk}
    # A peer disappearing after a successful call never erases its date debt.
    assert expired["waiting"].get(str(present_female.pk)) is None


@pytest.mark.django_db(transaction=True)
def test_historical_pair_is_never_retried_and_waiting_reasons_are_truthful(live_event):
    event, round_obj, now = live_event
    male = _person(event, "M", "Male")
    female = _person(event, "F", "Female")
    Pair.objects.create(
        event=event,
        round=round_obj,
        participant_a=male,
        participant_b=female,
        room_id="canceled-history",
        status=PairStatus.CANCELLED,
        live_allocated=True,
    )
    _ready(male, female, now=now)
    result = reconcile_live_round(event, round_obj, now=now)
    assert result["new_pairs"] == []
    assert result["waiting"] == {
        str(male.pk): "no_compatible_partner",
        str(female.pk): "no_compatible_partner",
    }

    event.male_date_limit = 0
    event.save(update_fields=["male_date_limit"])
    result = reconcile_live_round(event, round_obj, now=now + timedelta(seconds=1))
    assert result["waiting"][str(male.pk)] == "limit_reached"


@pytest.mark.django_db(transaction=True)
def test_default_four_and_eight_successful_date_caps(live_event):
    event, round_obj, now = live_event
    capped_male = _person(event, "Capped male", "Male")
    capped_female = _person(event, "Capped female", "Female")
    for index in range(12):
        historical_round = Round.objects.create(
            event=event,
            number=index + 2,
            starts_at=now - timedelta(days=index + 2),
            ends_at=now - timedelta(days=index + 2) + timedelta(minutes=5),
        )
        if index < 4:
            partner = _person(event, f"Male partner {index}", "Female")
            a, b = capped_male, partner
        else:
            partner = _person(event, f"Female partner {index}", "Male")
            a, b = partner, capped_female
        Pair.objects.create(
            event=event,
            round=historical_round,
            participant_a=a,
            participant_b=b,
            room_id=f"successful-cap-{index}",
            status=PairStatus.CANCELLED,
            connection_type_a="direct",
            connection_type_b="relay",
            media_received_a=True,
            media_received_b=True,
        )
    _ready(capped_male, capped_female, now=now)

    result = reconcile_live_round(event, round_obj, now=now)
    assert result["new_pairs"] == []
    assert result["waiting"] == {
        str(capped_male.pk): "limit_reached",
        str(capped_female.pk): "limit_reached",
    }


@pytest.mark.django_db(transaction=True)
def test_too_early_invocation_leaves_static_schedule_pair_untouched(live_event):
    event, round_obj, now = live_event
    male = _person(event, "Static male", "Male")
    female = _person(event, "Static female", "Female")
    round_obj.starts_at = now + timedelta(minutes=1)
    round_obj.ends_at = now + timedelta(minutes=6)
    round_obj.save(update_fields=["starts_at", "ends_at"])
    static_pair = Pair.objects.create(
        event=event,
        round=round_obj,
        participant_a=male,
        participant_b=female,
        room_id="static-future",
        status=PairStatus.PENDING,
    )
    _ready(male, female, now=now)

    early = reconcile_live_round(
        event, round_obj, now=round_obj.starts_at - timedelta(seconds=21)
    )
    assert early["new_pairs"] == []
    assert Pair.objects.filter(pk=static_pair.pk).exists()

    preconnect = reconcile_live_round(
        event, round_obj, now=round_obj.starts_at - timedelta(seconds=20)
    )
    assert not Pair.objects.filter(pk=static_pair.pk).exists()
    assert len(preconnect["new_pairs"]) == 1
