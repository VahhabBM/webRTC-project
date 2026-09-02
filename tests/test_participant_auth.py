import hashlib
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.events.auth import (
    InvalidJoinToken,
    authenticate_join_token,
    issue_join_token,
    personal_join_link,
    resolve_participant_from_scope,
)
from apps.events.models import Event, Participant


@pytest.fixture
def event(db):
    return Event.objects.create(
        name="Auth event",
        num_rounds=2,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(minutes=1),
        start_time=timezone.now() + timedelta(hours=1),
    )


@pytest.fixture
def participant(event):
    return Participant.objects.create(
        event=event, display_name="Alex", join_token_hash="!"
    )


@pytest.fixture
def issued_token(participant):
    return issue_join_token(participant)


@pytest.mark.django_db
def test_valid_personal_link_authenticates_correct_participant(
    participant, issued_token
):
    response = Client().get(personal_join_link(participant, issued_token))
    assert response.status_code == 200
    assert response.json()["participant"]["id"] == str(participant.pk)


@pytest.mark.django_db
def test_invalid_token_is_clear_and_does_not_leak_details(participant):
    response = Client().get("/join/p1_not-a-real-token/")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "join_token_invalid"
    assert "hash" not in response.content.decode().lower()


@pytest.mark.django_db
def test_expired_token_is_distinguished(participant, issued_token):
    participant.join_token_expires_at = timezone.now() - timedelta(seconds=1)
    participant.save(update_fields=["join_token_expires_at"])
    response = Client().get(personal_join_link(participant, issued_token))
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "join_token_expired"


@pytest.mark.django_db
def test_raw_token_is_not_persisted(participant):
    raw = issue_join_token(participant)
    participant.refresh_from_db()
    assert raw not in participant.join_token_hash
    assert raw not in participant.join_token_digest
    assert participant.join_token_digest == hashlib.sha256(raw.encode()).hexdigest()


@pytest.mark.django_db
def test_session_persists_and_identity_cannot_be_changed(participant, event):
    raw = issue_join_token(participant)
    client = Client()
    assert client.get(personal_join_link(participant, raw)).status_code == 200
    response = client.get("/participant/me/")
    assert response.json()["participant"]["id"] == str(participant.pk)
    assert client.get("/participant/me/").json()["participant"]["id"] == str(
        participant.pk
    )
    # A request cannot select an identity through a participant_id query parameter.
    response = client.get(f"/participant/me/?participant_id={participant.pk}")
    assert response.json()["participant"]["id"] == str(participant.pk)


@pytest.mark.django_db
def test_websocket_hook_resolves_session_participant(participant, issued_token):
    client = Client()
    client.get(personal_join_link(participant, issued_token))
    assert resolve_participant_from_scope({"session": client.session}) == participant


@pytest.mark.django_db
def test_token_is_reusable_until_expiry(participant, issued_token):
    assert authenticate_join_token(issued_token) == participant
    assert authenticate_join_token(issued_token) == participant


@pytest.mark.django_db
def test_malformed_token_is_rejected(participant):
    with pytest.raises(InvalidJoinToken):
        authenticate_join_token("not-a-token")
