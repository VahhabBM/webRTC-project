from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Event, Participant
from apps.registration.models import EmailVerificationToken


class RegistrationStatus:
    PENDING = "pending"
    CONFIRMED = "confirmed"
    WAITLISTED = "waitlisted"


def _payload(number: int, *, gender="Female", preference="Male"):
    return {
        "display_name": f"Person {number}",
        "email": f"person{number}@example.com",
        "age": 31,
        "country": "DE",
        "gender": gender,
        "want_to_date": preference,
        "whatsapp": "+49 123456789",
        "consents": {
            "age_confirmation": True,
            "sensitive_data_processing": True,
            "mutual_contact_sharing": True,
            "terms_and_privacy": True,
        },
    }


def _post_registration(event_id, number: int, **kwargs):
    return Client().post(
        reverse("registration:register", args=[event_id]),
        data=json.dumps(_payload(number, **kwargs)),
        content_type="application/json",
    )


@pytest.mark.django_db
@override_settings(EVENT_REGISTRATION_CAPACITY=2)
def test_capacity_reserves_pending_seats_and_keeps_verified_waitlist_status():
    event = Event.objects.create(
        name="Capacity", start_time=timezone.now() + timedelta(days=2)
    )

    first = _post_registration(event.pk, 1)
    second = _post_registration(event.pk, 2)
    third = _post_registration(event.pk, 3)

    assert [first.status_code, second.status_code, third.status_code] == [201, 201, 201]
    assert third.json()["registration_status"] == RegistrationStatus.WAITLISTED
    p1 = Participant.objects.get(email="person1@example.com")
    p2 = Participant.objects.get(email="person2@example.com")
    p3 = Participant.objects.get(email="person3@example.com")
    assert p1.registration_status == RegistrationStatus.PENDING
    assert p2.registration_status == RegistrationStatus.PENDING
    assert p1.reservation_expires_at > timezone.now()
    assert p3.registration_status == RegistrationStatus.WAITLISTED

    token1 = EmailVerificationToken.objects.get(participant=p1)
    token3 = EmailVerificationToken.objects.get(participant=p3)
    Client().get(reverse("registration:verify-email", args=[token1.token]))
    waitlist_response = Client().get(
        reverse("registration:verify-email", args=[token3.token])
    )
    p1.refresh_from_db()
    p3.refresh_from_db()

    assert p1.registration_status == RegistrationStatus.CONFIRMED
    assert p1.email_verified_at is not None
    assert p3.registration_status == RegistrationStatus.WAITLISTED
    assert "waitlist" in waitlist_response.content.decode().lower()


@pytest.mark.django_db
@override_settings(EVENT_REGISTRATION_CAPACITY=1)
def test_expired_unverified_reservation_does_not_consume_capacity():
    event = Event.objects.create(
        name="Expires", start_time=timezone.now() + timedelta(days=2)
    )
    Participant.objects.create(
        event=event,
        display_name="Expired reservation",
        email="expired@example.com",
        registration_status=RegistrationStatus.PENDING,
        reservation_expires_at=timezone.now() - timedelta(seconds=1),
    )

    response = _post_registration(event.pk, 1)

    assert response.status_code == 201
    assert response.json()["registration_status"] == RegistrationStatus.PENDING


@pytest.mark.django_db
def test_registration_rejects_unknown_gender_preference_values():
    event = Event.objects.create(
        name="Validation", start_time=timezone.now() + timedelta(days=2)
    )

    invalid_gender = _post_registration(event.pk, 1, gender="surprise")
    invalid_preference = _post_registration(event.pk, 2, preference="everyone")

    assert invalid_gender.status_code == 400
    assert invalid_gender.json()["code"] == "invalid_gender"
    assert invalid_preference.status_code == 400
    assert invalid_preference.json()["code"] == "invalid_date_preference"
    assert not Participant.objects.filter(event=event).exists()


@pytest.mark.django_db(transaction=True)
@override_settings(EVENT_REGISTRATION_CAPACITY=2)
def test_parallel_registrations_cannot_oversell_the_last_seat():
    if not connection.features.has_select_for_update:
        pytest.skip("Row-level locking is provided by production PostgreSQL.")

    event = Event.objects.create(
        name="Concurrent", start_time=timezone.now() + timedelta(days=2)
    )
    Participant.objects.create(
        event=event,
        display_name="Already reserved",
        email="already@example.com",
        registration_status=RegistrationStatus.PENDING,
        reservation_expires_at=timezone.now() + timedelta(hours=1),
    )

    def submit(number):
        close_old_connections()
        try:
            return _post_registration(event.pk, number).json()["registration_status"]
        finally:
            close_old_connections()

    with patch("apps.registration.views.EmailService.send_verification_email"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(submit, (1, 2)))

    assert sorted(statuses) == sorted(
        [RegistrationStatus.PENDING, RegistrationStatus.WAITLISTED]
    )
    assert Participant.objects.filter(
        event=event,
        registration_status__in=[
            RegistrationStatus.PENDING,
            RegistrationStatus.CONFIRMED,
        ],
    ).count() == 2
