from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.events.auth import SESSION_PARTICIPANT_KEY
from apps.events.models import Event, Participant


def _login_as_participant(client, participant):
    session = client.session
    session[SESSION_PARTICIPANT_KEY] = str(participant.pk)
    session.save()


@pytest.mark.django_db
def test_intro_video_state_is_private_persistent_and_server_gated(client):
    event = Event.objects.create(
        name="Intro flow",
        start_time=timezone.now() + timedelta(minutes=5),
        intro_video_url="https://cdn.example.com/intro.mp4",
        intro_video_duration_seconds=60,
    )
    participant = Participant.objects.create(
        event=event,
        display_name="Participant",
        email="participant@example.com",
    )
    me_url = reverse("participant-me")
    complete_url = reverse("event-intro-complete")

    unauthenticated = client.get(me_url)
    assert unauthenticated.status_code == 401

    _login_as_participant(client, participant)
    initial = client.get(me_url)
    data = initial.json()["participant"]
    assert initial.status_code == 200
    assert data["intro_video"]["url"] == event.intro_video_url
    assert data["intro_video"]["completed"] is False
    assert client.post(complete_url).status_code == 409

    event.start_time = timezone.now() - timedelta(seconds=1)
    event.save(update_fields=["start_time"])
    completed = client.post(complete_url)
    participant.refresh_from_db()
    reloaded = client.get(me_url)

    assert completed.status_code == 200
    assert participant.intro_video_completed_at is not None
    assert reloaded.json()["participant"]["intro_video"]["completed"] is True
    assert client.post(complete_url).status_code == 200


@pytest.mark.django_db
def test_no_intro_video_is_reported_as_not_configured(client):
    event = Event.objects.create(
        name="No intro", start_time=timezone.now() + timedelta(minutes=5)
    )
    participant = Participant.objects.create(
        event=event,
        display_name="Participant",
        email="participant@example.com",
    )
    _login_as_participant(client, participant)

    response = client.get(reverse("participant-me"))

    assert response.status_code == 200
    intro = response.json()["participant"]["intro_video"]
    assert intro["url"] == ""
    assert intro["configured"] is False
    assert intro["completed"] is True
    assert intro["event_start_at"] == event.start_time.isoformat()


@pytest.mark.django_db
def test_video_room_has_an_accessible_intro_stage(client):
    event = Event.objects.create(
        name="Intro markup",
        start_time=timezone.now() + timedelta(minutes=5),
        intro_video_url="https://cdn.example.com/intro.mp4",
        intro_video_duration_seconds=60,
    )
    participant = Participant.objects.create(
        event=event,
        display_name="Participant",
        email="participant@example.com",
    )
    _login_as_participant(client, participant)

    response = client.get(reverse("video-room"))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'id="eventIntro"' in html
    assert 'id="introVideo"' in html
    assert "Continue without video" in html
