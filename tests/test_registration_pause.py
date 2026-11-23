import hashlib
import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.events.models import (
    Event,
    EventStatus,
    Participant,
    RegistrationStatus,
    Round,
    RoundStatus,
    WaitlistInvitation,
)

User = get_user_model()


def _event():
    return Event.objects.create(
        name="Registration control",
        status=EventStatus.SCHEDULED,
        num_rounds=3,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(seconds=30),
        start_time=timezone.now() + timedelta(days=1),
    )


def _admin_client():
    user = User.objects.create_superuser(
        username="registration-admin",
        email="admin@example.com",
        password="test-pass",
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_pause_toggle_is_staff_change_protected_post_only_and_preserves_event_data():
    event = _event()
    participant = Participant.objects.create(
        event=event,
        display_name="Already registered",
        email="existing@example.com",
        registration_status=RegistrationStatus.CONFIRMED,
    )
    round_ = Round.objects.create(
        event=event,
        number=1,
        starts_at=event.start_time,
        ends_at=event.start_time + event.round_duration,
        status=RoundStatus.SCHEDULED,
    )
    url = reverse("admin:events_event_toggle_registration_pause", args=[event.pk])
    client = _admin_client()

    get_response = client.get(url)
    event.refresh_from_db()
    assert get_response.status_code == 405
    assert event.registration_paused is False

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(User.objects.get(username="registration-admin"))
    csrf_response = csrf_client.post(url)
    event.refresh_from_db()
    assert csrf_response.status_code == 403
    assert event.registration_paused is False

    response = client.post(url, {"registration_paused": "1"})
    event.refresh_from_db()
    assert response.status_code == 302
    assert event.registration_paused is True
    assert event.status == EventStatus.SCHEDULED
    assert event.rounds.get().pk == round_.pk
    assert event.participants.get().pk == participant.pk

    # Repeating the same POST must not accidentally reopen registrations.
    assert client.post(url, {"registration_paused": "1"}).status_code == 302
    event.refresh_from_db()
    assert event.registration_paused is True

    response = client.post(url, {"registration_paused": "0"})
    event.refresh_from_db()
    assert response.status_code == 302
    assert event.registration_paused is False
    assert event.status == EventStatus.SCHEDULED
    assert event.participants.get().pk == participant.pk


@pytest.mark.django_db
def test_toggle_requires_event_change_permission():
    event = _event()
    staff = User.objects.create_user(
        username="limited-staff", password="test-pass", is_staff=True
    )
    client = Client()
    client.force_login(staff)

    response = client.post(
        reverse("admin:events_event_toggle_registration_pause", args=[event.pk])
    )

    event.refresh_from_db()
    assert response.status_code == 403
    assert event.registration_paused is False


@pytest.mark.django_db
def test_paused_registration_form_shows_closed_notice_and_disables_submit():
    event = _event()
    event.registration_paused = True
    event.save(update_fields=["registration_paused"])

    response = Client().get(reverse("registration:register-ui", args=[event.pk]))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Registration for this event is temporarily closed" in content
    assert 'class="submit-btn" disabled' in content
    assert "Meetakk Matching Form" in content


@pytest.mark.django_db
def test_paused_registration_rejected_before_creating_participant_or_receipt():
    event = _event()
    event.registration_paused = True
    event.save(update_fields=["registration_paused"])
    client = Client()
    payload = {
        "display_name": "New person",
        "email": "new@example.com",
        "email_confirmation": "new@example.com",
        "age": 30,
        "country": "GB",
        "consents": {
            "age_confirmation": True,
            "sensitive_data_processing": True,
            "mutual_contact_sharing": True,
            "terms_and_privacy": True,
        },
    }

    response = client.post(
        reverse("registration:register", args=[event.pk]),
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 409
    assert response.json()["code"] == "registration_paused"
    assert not Participant.objects.filter(event=event, email="new@example.com").exists()
    _admin_client().post(
        reverse("admin:events_event_toggle_registration_pause", args=[event.pk]),
        {"registration_paused": "0"},
    )
    reopened = client.post(
        reverse("registration:register", args=[event.pk]),
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert reopened.status_code == 201
    assert (
        Participant.objects.get(
            event=event, email="new@example.com"
        ).registration_receipt_sent_at
        is not None
    )


@pytest.mark.django_db
def test_paused_event_does_not_accept_a_waitlist_invitation():
    event = _event()
    event.registration_paused = True
    event.save(update_fields=["registration_paused"])
    participant = Participant.objects.create(
        event=event,
        display_name="Waitlisted person",
        email="waitlisted@example.com",
        gender="Female",
        registration_status=RegistrationStatus.WAITLISTED,
    )
    token = "one-time-private-token"
    invitation = WaitlistInvitation.objects.create(
        event=event,
        participant=participant,
        token_digest=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=timezone.now() + timedelta(days=1),
    )
    url = reverse("registration:accept-waitlist-invitation", args=[token])

    preview = Client().get(url)
    response = Client().post(url)

    participant.refresh_from_db()
    invitation.refresh_from_db()
    assert preview.status_code == 200
    assert "Registration is paused" in preview.content.decode()
    assert "Accept invitation" not in preview.content.decode()
    assert response.status_code == 409
    assert participant.registration_status == RegistrationStatus.WAITLISTED
    assert invitation.accepted_at is None
