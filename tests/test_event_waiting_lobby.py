from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.events.models import Event, Participant


@pytest.mark.django_db
def test_authenticated_participant_gets_english_pre_event_waiting_lobby():
    event = Event.objects.create(
        name="Upcoming networking night",
        num_rounds=2,
        round_duration=timedelta(minutes=8),
        break_duration=timedelta(minutes=2),
        start_time=timezone.now() + timedelta(hours=2),
    )
    participant = Participant.objects.create(
        event=event,
        display_name="Test Participant",
        join_token_hash="!",
    )
    client = Client()
    session = client.session
    session["participant_id"] = str(participant.pk)
    session.save()

    response = client.get("/room/")

    assert response.status_code == 200
    content = response.content.decode()
    assert 'id="eventWaitingLobby"' in content
    assert "Your speed dating starts soon" in content
    assert 'id="waitingStatus"' in content
    assert "waitForFreshEventStart" in content
    assert "fetchCurrentParticipant" in content
    assert "Time until your event starts" in content
    assert "Keep this page open." in content
    assert "await waitForEventStart(" in content
