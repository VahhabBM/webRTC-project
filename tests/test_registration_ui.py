from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.events.models import Event, Participant, ParticipantStatus, Tag
from apps.registration.models import EmailVerificationToken


@pytest.mark.django_db
class TestRegistrationUI:
    def test_registration_page_renders_html_with_event_and_tags(self, client: Client):
        event = Event.objects.create(
            name="Global Founders Meetup 2026",
            num_rounds=2,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=timezone.now(),
        )
        tag = Tag.objects.create(name="Robotics")

        response = client.get(f"/registration/{event.id}/")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert 'dir="ltr"' in content
        assert event.name in content
        assert tag.name in content
        assert "tag-search" in content

    def test_check_email_page_renders_successfully(self, client: Client):
        response = client.get("/registration/check-email/?email=user@example.com")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Check Your Inbox" in content
        assert "user@example.com" in content

    def test_verify_email_renders_html_success_page(self, client: Client):
        event = Event.objects.create(
            name="Design Summit",
            num_rounds=2,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=timezone.now(),
        )
        participant = Participant.objects.create(
            event=event,
            display_name="Sarah Connor",
            email="sarah@example.com",
            status=ParticipantStatus.WAITING,
        )
        token = EmailVerificationToken.create_for(participant)

        response = client.get(f"/registration/verify/{token.token}/")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "You are Confirmed!" in content
        assert participant.display_name in content

    def test_verify_email_renders_expired_page_on_invalid_token(self, client: Client):
        response = client.get("/registration/verify/invalid-fake-token/")
        assert response.status_code == 404
