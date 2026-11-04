from __future__ import annotations

import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.events.models import (
    Event,
    Participant,
)


class RegistrationStatus:
    PENDING = "pending"
    CONFIRMED = "confirmed"
    WAITLISTED = "waitlisted"

User = get_user_model()


def _participant(event, number, *, gender="Female", country="DE", status=None):
    return Participant.objects.create(
        event=event,
        display_name=f"Person {number}",
        email=f"person{number}@example.com",
        gender=gender,
        country=country,
        email_verified_at=timezone.now(),
        registration_status=status or RegistrationStatus.CONFIRMED,
    )


def _admin_client():
    user = User.objects.create_superuser(
        username="event-admin",
        email="admin@example.com",
        password="test-pass",
    )
    client = Client()
    client.force_login(user)
    return client, user


def _invitations():
    return apps.get_model("events", "WaitlistInvitation").objects


@pytest.mark.django_db
def test_registration_statistics_include_unknown_in_denominator_and_hide_people():
    event = Event.objects.create(
        name="Stats", start_time=timezone.now() + timedelta(days=1)
    )
    _participant(event, 1, gender="Male", country="DE")
    _participant(event, 2, gender="Male", country="DE")
    _participant(event, 3, gender="Female", country="FR")
    _participant(event, 4, gender="", country="")
    client, _ = _admin_client()

    response = client.get(
        f"/admin/events/event/{event.pk}/registration-stats/"
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert "Male" in html and "50.0%" in html
    assert "Female" in html and "25.0%" in html
    assert "Not provided" in html
    assert "person1@example.com" not in html
    assert "person4@example.com" not in html


@pytest.mark.django_db
def test_registration_statistics_require_event_permissions():
    event = Event.objects.create(
        name="Private stats", start_time=timezone.now() + timedelta(days=1)
    )
    staff = User.objects.create_user(
        username="staff-without-event-permissions",
        password="test-pass",
        is_staff=True,
    )
    client = Client()
    client.force_login(staff)

    response = client.get(
        f"/admin/events/event/{event.pk}/registration-stats/"
    )

    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(EVENT_REGISTRATION_CAPACITY=2)
def test_verified_waitlisted_woman_can_accept_one_use_admin_invitation():
    event = Event.objects.create(
        name="Waitlist", start_time=timezone.now() + timedelta(days=1)
    )
    invited = _participant(
        event, 1, gender="Female", status=RegistrationStatus.WAITLISTED
    )
    _participant(event, 2, gender="Male")
    client, admin = _admin_client()
    invite_url = (
        f"/admin/events/event/{event.pk}/waitlist/{invited.pk}/invite/"
    )

    with patch("apps.events.admin.EmailService.send_waitlist_invitation_email") as send:
        response = client.post(invite_url)

    assert response.status_code == 302
    invitation = _invitations().get(participant=invited)
    assert invitation.created_by == admin
    assert invitation.token_digest
    sent_body = send.call_args.kwargs["message"]
    token = re.search(r"/registration/waitlist/([^/]+)/", sent_body).group(1)
    assert token not in invitation.token_digest

    accept_url = reverse("registration:accept-waitlist-invitation", args=[token])
    page = Client().get(accept_url)
    assert page.status_code == 200
    assert invitation.accepted_at is None

    accepted = Client().post(accept_url)
    invited.refresh_from_db()
    invitation.refresh_from_db()
    replay = Client().post(accept_url)

    assert accepted.status_code == 200
    assert invited.registration_status == RegistrationStatus.CONFIRMED
    assert invitation.accepted_at is not None
    assert replay.status_code == 410


@pytest.mark.django_db
def test_admin_cannot_invite_unverified_or_non_female_waitlist_entries():
    event = Event.objects.create(
        name="Eligibility", start_time=timezone.now() + timedelta(days=1)
    )
    male = _participant(event, 1, gender="Male", status=RegistrationStatus.WAITLISTED)
    unverified = Participant.objects.create(
        event=event,
        display_name="Unverified",
        email="unverified@example.com",
        gender="Female",
        registration_status=RegistrationStatus.WAITLISTED,
    )
    client, _ = _admin_client()

    with patch("apps.events.admin.EmailService.send_waitlist_invitation_email") as send:
        male_response = client.post(
            f"/admin/events/event/{event.pk}/waitlist/{male.pk}/invite/"
        )
        unverified_response = client.post(
            f"/admin/events/event/{event.pk}/waitlist/{unverified.pk}/invite/"
        )

    assert male_response.status_code == 404
    assert unverified_response.status_code == 404
    assert send.call_count == 0
    assert not _invitations().exists()


@pytest.mark.django_db
@override_settings(EVENT_REGISTRATION_CAPACITY=1)
def test_invitation_stays_waitlisted_if_capacity_is_full_at_acceptance():
    event = Event.objects.create(
        name="Capacity race", start_time=timezone.now() + timedelta(days=1)
    )
    invited = _participant(
        event, 1, gender="Female", status=RegistrationStatus.WAITLISTED
    )
    client, _ = _admin_client()
    with patch("apps.events.admin.EmailService.send_waitlist_invitation_email") as send:
        client.post(f"/admin/events/event/{event.pk}/waitlist/{invited.pk}/invite/")
    invitation = _invitations().get(participant=invited)
    token = re.search(r"/registration/waitlist/([^/]+)/", send.call_args.kwargs["message"]).group(1)

    _participant(event, 2, gender="Male")
    response = Client().post(
        reverse("registration:accept-waitlist-invitation", args=[token])
    )
    invited.refresh_from_db()
    invitation.refresh_from_db()

    assert response.status_code == 409
    assert invited.registration_status == RegistrationStatus.WAITLISTED
    assert invitation.accepted_at is None
