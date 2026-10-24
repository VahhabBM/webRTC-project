from datetime import timedelta

import pytest
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.events.auth import SESSION_PARTICIPANT_KEY
from apps.events.interest import (
    INTEREST_RESPONSE_WINDOW,
    PairInterestError,
    submit_pair_interest,
)
from apps.events.models import Event, Pair, PairInterest, Participant, Round


@pytest.fixture
def matched_pair(db):
    now = timezone.now()
    event = Event.objects.create(
        name="Contact Sharing Test",
        num_rounds=1,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(),
        start_time=now - timedelta(minutes=5),
    )
    round_obj = Round.objects.create(
        event=event,
        number=1,
        starts_at=now - timedelta(minutes=5),
        ends_at=now - timedelta(seconds=2),
    )
    first = Participant.objects.create(
        event=event,
        display_name="Alex",
        email="alex@example.com",
        whatsapp="+12025550101",
        join_token_hash="!",
    )
    second = Participant.objects.create(
        event=event,
        display_name="Emilia",
        email="emilia@example.com",
        whatsapp="+12025550102",
        join_token_hash="!",
    )
    pair = Pair.objects.create(
        event=event,
        round=round_obj,
        participant_a=first,
        participant_b=second,
        room_id="contact-sharing-room",
    )
    return event, round_obj, pair, first, second


@pytest.fixture(autouse=True)
def locmem_email_backend(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox.clear()


def _authenticated_client(participant):
    client = Client(enforce_csrf_checks=True)
    session = client.session
    session[SESSION_PARTICIPANT_KEY] = str(participant.pk)
    session.save()
    cache.delete(f"participant:session:{participant.pk}")
    response = client.get("/room/")
    assert response.status_code == 200
    return client


def _submit(client, *, room_id, interested, round_number=1):
    csrf_token = client.cookies["csrftoken"].value
    return client.post(
        "/api/round-interest/",
        data={
            "room_id": room_id,
            "round_number": round_number,
            "interested": interested,
        },
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token,
    )


@pytest.mark.django_db
def test_whatsapp_details_are_revealed_only_after_both_choose_yes(matched_pair):
    _event, _round_obj, pair, first, second = matched_pair
    first_client = _authenticated_client(first)
    second_client = _authenticated_client(second)

    first_response = _submit(first_client, room_id=pair.room_id, interested=True)
    assert first_response.status_code == 200
    assert first_response.json()["recorded"] is True
    assert first_response.json()["mutual"] is False
    assert first_response.json()["partner_whatsapp"] is None
    assert mail.outbox == []

    second_response = _submit(second_client, room_id=pair.room_id, interested=True)
    assert second_response.status_code == 200
    assert second_response.json()["mutual"] is True
    assert second_response.json()["partner_display_name"] == "Alex"
    assert second_response.json()["partner_whatsapp"] == first.whatsapp
    assert mail.outbox == []

    repeated = _submit(second_client, room_id=pair.room_id, interested=True)
    assert repeated.status_code == 200
    assert repeated.json()["partner_whatsapp"] == first.whatsapp
    assert mail.outbox == []


@pytest.mark.django_db
def test_no_contact_details_are_shared_if_either_person_says_no(matched_pair):
    _event, _round_obj, pair, first, second = matched_pair
    first_client = _authenticated_client(first)
    second_client = _authenticated_client(second)

    assert (
        _submit(first_client, room_id=pair.room_id, interested=True).status_code == 200
    )
    response = _submit(second_client, room_id=pair.room_id, interested=False)

    assert response.status_code == 200
    assert response.json()["mutual"] is False
    assert response.json()["partner_whatsapp"] is None
    assert mail.outbox == []
    choice = PairInterest.objects.get(pair=pair)
    assert choice.response_for(first) is True
    assert choice.response_for(second) is False


@pytest.mark.django_db
def test_vote_is_bound_to_pair_member_and_choice_is_immutable(matched_pair):
    _event, _round_obj, pair, first, second = matched_pair
    first_client = _authenticated_client(first)

    assert (
        _submit(first_client, room_id="another-room", interested=True).status_code
        == 404
    )
    assert (
        _submit(first_client, room_id=pair.room_id, interested=True).status_code == 200
    )
    conflict = _submit(first_client, room_id=pair.room_id, interested=False)
    assert conflict.status_code == 409
    assert PairInterest.objects.get(pair=pair).response_for(first) is True
    assert mail.outbox == []


@pytest.mark.django_db
def test_choice_window_is_enforced_by_server_clock(matched_pair):
    _event, round_obj, pair, first, _second = matched_pair

    with pytest.raises(PairInterestError) as early:
        submit_pair_interest(
            participant=first,
            room_id=pair.room_id,
            round_number=1,
            interested=True,
            now=round_obj.ends_at - timedelta(milliseconds=1),
        )
    assert early.value.status_code == 409

    with pytest.raises(PairInterestError) as expired:
        submit_pair_interest(
            participant=first,
            room_id=pair.room_id,
            round_number=1,
            interested=True,
            now=round_obj.ends_at
            + INTEREST_RESPONSE_WINDOW
            + timedelta(microseconds=1),
        )
    assert expired.value.status_code == 410
    assert not PairInterest.objects.filter(pair=pair).exists()


@pytest.mark.django_db
def test_no_contact_email_is_sent_after_mutual_yes(matched_pair):
    _event, _round_obj, pair, first, second = matched_pair
    submit_pair_interest(
        participant=first,
        room_id=pair.room_id,
        round_number=1,
        interested=True,
    )
    result = submit_pair_interest(
        participant=second,
        room_id=pair.room_id,
        round_number=1,
        interested=True,
    )
    assert result["mutual"] is True
    assert result["partner_whatsapp"] == first.whatsapp
    assert mail.outbox == []
