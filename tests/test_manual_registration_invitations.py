from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Event, EventStatus


def _admin_client():
    user = get_user_model().objects.create_superuser(
        username="invite-admin",
        email="admin@example.com",
        password="test-pass",
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_event_admin_can_send_registration_link_to_selected_unique_addresses():
    event = Event.objects.create(
        name="Selected invitation test",
        status=EventStatus.REGISTRATION_OPEN,
        start_time=timezone.now() + timedelta(days=3),
    )
    client = _admin_client()
    url = reverse("admin:events_event_registration_invitations", args=[event.pk])

    response = client.post(
        url,
        {"recipients": "first@example.com, second@example.com\nFIRST@example.com"},
    )

    assert response.status_code == 200
    assert len(mail.outbox) == 2
    expected_link = f"http://testserver/registration/{event.pk}/"
    assert all(expected_link in message.body for message in mail.outbox)
    assert {message.to[0] for message in mail.outbox} == {
        "first@example.com",
        "second@example.com",
    }
    assert not event.participants.exists()


@pytest.mark.django_db
def test_registration_invitation_rejects_invalid_addresses_without_sending():
    event = Event.objects.create(
        name="Invalid invitation test",
        status=EventStatus.REGISTRATION_OPEN,
        start_time=timezone.now() + timedelta(days=3),
    )
    client = _admin_client()
    url = reverse("admin:events_event_registration_invitations", args=[event.pk])

    response = client.post(url, {"recipients": "valid@example.com, invalid-address"})

    assert response.status_code == 200
    assert not mail.outbox
    assert b"Enter valid email addresses" in response.content


@pytest.mark.django_db
def test_registration_invitations_are_not_sent_for_closed_events():
    event = Event.objects.create(
        name="Closed invitation test",
        status=EventStatus.CANCELLED,
        start_time=timezone.now() + timedelta(days=3),
    )
    client = _admin_client()
    url = reverse("admin:events_event_registration_invitations", args=[event.pk])

    response = client.post(url, {"recipients": "person@example.com"})

    assert response.status_code == 200
    assert not mail.outbox
    assert b"only for events that are scheduled or open" in response.content
