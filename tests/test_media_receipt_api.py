from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.events.auth import issue_join_token
from apps.events.models import Event, Pair, Participant, Round


@pytest.fixture
def media_case(db):
    now = timezone.now()
    event = Event.objects.create(name="Media receipt rehearsal", start_time=now, num_rounds=1)
    round_obj = Round.objects.create(event=event, number=1, starts_at=now, ends_at=now + timedelta(minutes=5))
    people = [Participant.objects.create(event=event, display_name=name) for name in ("A", "B")]
    people.sort(key=lambda p: p.pk)
    pair = Pair.objects.create(event=event, round=round_obj, participant_a=people[0], participant_b=people[1], room_id="receipt-check", live_allocated=True)
    client = Client()
    assert client.get(f"/join/{issue_join_token(people[0])}/").status_code == 200
    return pair, client


def report(client, pair, **metrics):
    return client.post("/api/telemetry/connection/", data={
        "room_id": pair.room_id, "connection_type": "relay", "connection_time_ms": 800,
        **metrics,
    }, content_type="application/json")


@pytest.mark.django_db
def test_ice_connection_does_not_count_as_received_media(media_case):
    pair, client = media_case
    assert report(client, pair).status_code == 200
    pair.refresh_from_db()
    assert pair.connection_type_a == "relay"
    assert not pair.media_received_a
    assert report(client, pair, audio_bytes_received=1000, video_frames_decoded=0).status_code == 200
    pair.refresh_from_db()
    assert not pair.media_received_a
    assert report(client, pair, audio_bytes_received=1000, video_frames_decoded=3).status_code == 200
    pair.refresh_from_db()
    assert pair.media_received_a
    assert not pair.media_received_b


@pytest.mark.django_db
def test_old_or_cancelled_rooms_cannot_submit_media_proof(media_case):
    pair, client = media_case
    pair.status = "cancelled"
    pair.save(update_fields=["status"])
    assert report(client, pair, audio_bytes_received=1000, video_frames_decoded=3).status_code == 404
    pair.refresh_from_db()
    assert not pair.media_received_a


@pytest.mark.django_db
@pytest.mark.parametrize("value", [True, -1, "5", None])
def test_media_counters_require_nonnegative_integers(media_case, value):
    pair, client = media_case
    assert report(client, pair, audio_bytes_received=value, video_frames_decoded=3).status_code == 400
