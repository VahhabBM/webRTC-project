"""Tests for the per-event unique_tags feature.

Covers all ten acceptance criteria:
 1. unique_tags defaults to False.
 2. unique_tags=False still allows multiple tags per participant.
 3. unique_tags=True accepts exactly one tag.
 4. unique_tags=True rejects multiple tags with a clear error.
 5. Multiple participants can select the same tag.
 6. Server-side enforcement through every relevant entry point.
 7. Existing events/selections are untouched by the migration default.
 8. Enabling unique_tags when participants already have multiple tags is blocked.
 9. Django Admin exposes unique_tags when creating and editing events.
10. Existing matching/scoring tests are unaffected (smoke check).
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.events.admin import EventAdmin, EventAdminForm
from apps.events.models import Event, EventStatus, Participant, ParticipantTag, Tag
from apps.registration.forms import RegistrationError, validate_registration_data

User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(unique_tags: bool = False, **kwargs) -> Event:
    defaults = dict(
        name="Test Event",
        status=EventStatus.DRAFT,
        num_rounds=2,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(minutes=1),
        start_time=timezone.now() + timedelta(hours=1),
        unique_tags=unique_tags,
    )
    defaults.update(kwargs)
    return Event.objects.create(**defaults)


def _make_participant(
    event: Event, name: str = "Alice", email: str = "alice@example.com"
) -> Participant:
    return Participant.objects.create(event=event, display_name=name, email=email)


def _make_tag(name: str) -> Tag:
    return Tag.objects.create(name=name)


def _registration_payload(**overrides) -> dict:
    base = {
        "display_name": "Test User",
        "email": "testuser@example.com",
        "age": 25,
        "tag_ids": [],
        "accepted_terms": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. unique_tags defaults to False
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_unique_tags_defaults_to_false():
    event = _make_event()
    assert event.unique_tags is False


# ---------------------------------------------------------------------------
# 2. unique_tags=False still allows multiple tags per participant
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_multiple_tags_allowed_when_unique_tags_disabled():
    event = _make_event(unique_tags=False)
    tag1 = _make_tag("Python")
    tag2 = _make_tag("Django")

    data = _registration_payload(
        tag_ids=[str(tag1.id), str(tag2.id)],
    )
    result = validate_registration_data(event, data)
    assert len(result["tags"]) == 2


# ---------------------------------------------------------------------------
# 3. unique_tags=True accepts exactly one tag
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_single_tag_accepted_when_unique_tags_enabled():
    event = _make_event(unique_tags=True)
    tag = _make_tag("AI")

    data = _registration_payload(tag_ids=[str(tag.id)])
    result = validate_registration_data(event, data)
    assert len(result["tags"]) == 1


# ---------------------------------------------------------------------------
# 4. unique_tags=True rejects multiple tags with clear validation feedback
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_multiple_tags_rejected_when_unique_tags_enabled():
    event = _make_event(unique_tags=True)
    tag1 = _make_tag("Rust")
    tag2 = _make_tag("Go")

    data = _registration_payload(tag_ids=[str(tag1.id), str(tag2.id)])
    with pytest.raises(RegistrationError) as exc_info:
        validate_registration_data(event, data)

    assert exc_info.value.code == "invalid_tag_count"
    # Error message must be non-empty and human-readable
    assert len(exc_info.value.message) > 0


@pytest.mark.django_db
def test_zero_tags_rejected_when_unique_tags_enabled():
    event = _make_event(unique_tags=True)

    data = _registration_payload(tag_ids=[])
    with pytest.raises(RegistrationError) as exc_info:
        validate_registration_data(event, data)

    assert exc_info.value.code == "invalid_tag_count"


# ---------------------------------------------------------------------------
# 5. Multiple participants can select the same tag
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_multiple_participants_can_share_same_tag():
    event = _make_event(unique_tags=True)
    tag = _make_tag("Blockchain")

    p1 = _make_participant(event, name="Alice", email="alice@example.com")
    p2 = _make_participant(event, name="Bob", email="bob@example.com")

    ParticipantTag.objects.create(participant=p1, tag=tag)
    ParticipantTag.objects.create(participant=p2, tag=tag)

    assert ParticipantTag.objects.filter(tag=tag).count() == 2


# ---------------------------------------------------------------------------
# 6. Server-side enforcement through the HTTP registration endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_http_endpoint_rejects_multiple_tags_when_unique_tags_enabled(client: Client):
    from unittest.mock import patch

    event = _make_event(unique_tags=True)
    tag1 = _make_tag("DevOps")
    tag2 = _make_tag("Cloud")

    payload = json.dumps(
        {
            "display_name": "Carol",
            "email": "carol@example.com",
            "age": 25,
            "tag_ids": [str(tag1.id), str(tag2.id)],
            "accepted_terms": True,
        }
    )

    with (
        patch("apps.registration.views.EmailService.send_verification_email"),
        patch("apps.registration.views.is_rate_limited", return_value=False),
    ):
        response = client.post(
            f"/registration/events/{event.id}/register/",
            data=payload,
            content_type="application/json",
        )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_tag_count"


@pytest.mark.django_db
def test_http_endpoint_accepts_single_tag_when_unique_tags_enabled(client: Client):
    from unittest.mock import patch

    event = _make_event(unique_tags=True)
    tag = _make_tag("Security")

    payload = json.dumps(
        {
            "display_name": "Dave",
            "email": "dave@example.com",
            "age": 25,
            "tag_ids": [str(tag.id)],
            "accepted_terms": True,
        }
    )

    with (
        patch("apps.registration.views.EmailService.send_verification_email"),
        patch("apps.registration.views.is_rate_limited", return_value=False),
    ):
        response = client.post(
            f"/registration/events/{event.id}/register/",
            data=payload,
            content_type="application/json",
        )

    assert response.status_code == 201


@pytest.mark.django_db
def test_http_endpoint_still_allows_multiple_tags_when_unique_tags_disabled(
    client: Client,
):
    from unittest.mock import patch

    event = _make_event(unique_tags=False)
    tag1 = _make_tag("ML")
    tag2 = _make_tag("Data")

    payload = json.dumps(
        {
            "display_name": "Eve",
            "email": "eve@example.com",
            "age": 25,
            "tag_ids": [str(tag1.id), str(tag2.id)],
            "accepted_terms": True,
        }
    )

    with (
        patch("apps.registration.views.EmailService.send_verification_email"),
        patch("apps.registration.views.is_rate_limited", return_value=False),
    ):
        response = client.post(
            f"/registration/events/{event.id}/register/",
            data=payload,
            content_type="application/json",
        )

    assert response.status_code == 201


# ---------------------------------------------------------------------------
# 7. Existing events retain unique_tags=False after migration
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_existing_event_has_unique_tags_false_by_default():
    """Events created without specifying unique_tags must default to False,
    preserving the original multi-tag behaviour."""
    event = Event.objects.create(
        name="Legacy Event",
        num_rounds=1,
        round_duration=timedelta(minutes=10),
        break_duration=timedelta(minutes=0),
        start_time=timezone.now(),
    )
    event.refresh_from_db()
    assert event.unique_tags is False


@pytest.mark.django_db
def test_existing_participant_tags_unaffected_by_field_addition():
    """Participant tag rows created before the migration are untouched."""
    event = _make_event(unique_tags=False)
    tag1 = _make_tag("IoT")
    tag2 = _make_tag("Edge")
    participant = _make_participant(event)
    ParticipantTag.objects.create(participant=participant, tag=tag1)
    ParticipantTag.objects.create(participant=participant, tag=tag2)

    # Enabling unique_tags on a fresh event should not touch other events'
    # participant data.
    other_event = _make_event(unique_tags=True, name="New Event")
    assert ParticipantTag.objects.filter(participant=participant).count() == 2
    assert other_event.unique_tags is True


# ---------------------------------------------------------------------------
# 8. Enabling unique_tags blocked when participants already have multiple tags
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_form_blocks_enabling_unique_tags_when_participants_have_multiple_tags():
    event = _make_event(unique_tags=False)
    tag1 = _make_tag("Frontend")
    tag2 = _make_tag("Backend")
    participant = _make_participant(event)
    ParticipantTag.objects.create(participant=participant, tag=tag1)
    ParticipantTag.objects.create(participant=participant, tag=tag2)

    form = EventAdminForm(
        data={
            "name": event.name,
            "description": "",
            "status": event.status,
            "start_time": event.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "num_rounds": event.num_rounds,
            "round_duration": "0:05:00",
            "break_duration": "0:01:00",
            "scoring_weights": '{"tag_overlap": 1.0}',
            "unique_tags": True,
        },
        instance=event,
    )
    assert not form.is_valid()
    errors = form.non_field_errors()
    assert len(errors) > 0
    assert "unique tag" in errors[0].lower() or "participant" in errors[0].lower()


@pytest.mark.django_db
def test_admin_form_allows_enabling_unique_tags_when_no_participant_has_multiple_tags():
    event = _make_event(unique_tags=False)
    tag = _make_tag("UX")
    participant = _make_participant(event)
    # Only one tag — enabling should be allowed.
    ParticipantTag.objects.create(participant=participant, tag=tag)

    form = EventAdminForm(
        data={
            "name": event.name,
            "description": "",
            "status": event.status,
            "start_time": event.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "num_rounds": event.num_rounds,
            "round_duration": "0:05:00",
            "break_duration": "0:01:00",
            "scoring_weights": '{"tag_overlap": 1.0}',
            "unique_tags": True,
        },
        instance=event,
    )
    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_admin_form_allows_enabling_unique_tags_on_new_event_with_no_participants():
    """Creating a brand-new event with unique_tags=True must succeed."""
    form = EventAdminForm(
        data={
            "name": "Brand New Event",
            "description": "",
            "status": EventStatus.DRAFT,
            "start_time": (timezone.now() + timedelta(hours=2)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "num_rounds": 2,
            "round_duration": "0:05:00",
            "break_duration": "0:01:00",
            "scoring_weights": '{"tag_overlap": 1.0}',
            "unique_tags": True,
        },
    )
    assert form.is_valid(), form.errors


# ---------------------------------------------------------------------------
# 9. Django Admin exposes unique_tags when creating and editing events
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_event_admin_fields_include_unique_tags():
    admin_instance = site._registry[Event]
    assert isinstance(admin_instance, EventAdmin)
    assert "unique_tags" in admin_instance.fields


@pytest.mark.django_db
def test_event_admin_change_page_renders_unique_tags_field(client: Client):
    admin_user = User.objects.create_superuser(
        username="admin_unique",
        email="admin_unique@test.com",
        password="adminpassword",
    )
    client.force_login(admin_user)
    event = _make_event()

    response = client.get(f"/admin/events/event/{event.id}/change/")
    assert response.status_code == 200
    content = response.content.decode("utf-8")
    assert "unique_tags" in content or "Unique tag selection" in content


@pytest.mark.django_db
def test_event_admin_add_page_renders_unique_tags_field(client: Client):
    admin_user = User.objects.create_superuser(
        username="admin_add",
        email="admin_add@test.com",
        password="adminpassword",
    )
    client.force_login(admin_user)

    response = client.get("/admin/events/event/add/")
    assert response.status_code == 200
    content = response.content.decode("utf-8")
    assert "unique_tags" in content or "Unique tag selection" in content


# ---------------------------------------------------------------------------
# 10. Existing matching/scoring behaviour is unaffected (smoke check)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_scoring_unaffected_by_unique_tags_field():
    """Adding unique_tags to Event must not break compute_match_score."""
    from apps.events.scoring import (
        ParticipantProfile,
        ScoringWeights,
        compute_match_score,
    )

    weights = ScoringWeights(tag_weight=1.0)
    profile_a = ParticipantProfile(tag_ids=frozenset(["tag1", "tag2"]))
    profile_b = ParticipantProfile(tag_ids=frozenset(["tag1", "tag3"]))

    score = compute_match_score(profile_a, profile_b, weights)
    # Jaccard({tag1,tag2} ∩ {tag1,tag3}) / union = 1/3
    assert abs(score - 1 / 3) < 1e-9


@pytest.mark.django_db
def test_build_participant_profile_unaffected_by_unique_tags():
    """build_participant_profile must work on events with unique_tags enabled."""
    from apps.events.scoring import build_participant_profile

    event = _make_event(unique_tags=True)
    tag = _make_tag("GraphQL")
    participant = _make_participant(event)
    ParticipantTag.objects.create(participant=participant, tag=tag)

    participant.refresh_from_db()
    profile = build_participant_profile(participant)
    assert str(tag.pk) in profile.tag_ids
