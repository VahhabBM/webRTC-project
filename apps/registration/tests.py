from __future__ import annotations

import json
from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from apps.events.models import Event, EventStatus, Participant, Tag

from .models import EmailVerificationToken


class RegistrationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.event = Event.objects.create(
            name="رویداد تست",
            status=EventStatus.DRAFT,
            num_rounds=3,
            round_duration=timedelta(minutes=7),
            break_duration=timedelta(minutes=1),
            start_time=timezone.now() + timedelta(hours=1),
        )
        self.tag1 = Tag.objects.create(name="تکنولوژی")
        self.tag2 = Tag.objects.create(name="کسب‌وکار")

    def _register(self, **overrides):
        payload = {
            "display_name": "علی رضایی",
            "email": "ali@example.com",
            "tag_ids": [str(self.tag1.id)],
            "accepted_terms": True,
        }
        payload.update(overrides)
        return self.client.post(
            f"/registration/events/{self.event.id}/register/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_successful_registration_creates_participant(self):
        response = self._register()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Participant.objects.count(), 1)
        participant = Participant.objects.first()
        self.assertEqual(participant.email, "ali@example.com")
        self.assertIsNone(participant.joined_at)

    def test_duplicate_email_in_same_event_is_rejected(self):
        self._register()
        response = self._register()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "duplicate_email")

    def test_registration_without_accepted_terms_is_rejected(self):
        response = self._register(accepted_terms=False)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "terms_not_accepted")

    def test_registration_with_too_many_tags_is_rejected(self):
        extra_tags = [Tag.objects.create(name=f"تگ{i}").id for i in range(6)]
        response = self._register(tag_ids=[str(t) for t in extra_tags])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_tag_count")

    def test_registration_creates_verification_token(self):
        self._register()
        participant = Participant.objects.first()
        self.assertTrue(
            EmailVerificationToken.objects.filter(participant=participant).exists()
        )


class VerifyEmailTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.event = Event.objects.create(
            name="رویداد تست",
            status=EventStatus.DRAFT,
            num_rounds=3,
            round_duration=timedelta(minutes=7),
            break_duration=timedelta(minutes=1),
            start_time=timezone.now() + timedelta(hours=1),
        )
        self.participant = Participant.objects.create(
            event=self.event, display_name="علی رضایی", email="ali@example.com"
        )
        self.token = EmailVerificationToken.create_for(self.participant)

    def test_valid_token_confirms_participant(self):
        response = self.client.get(f"/registration/verify/{self.token.token}/")
        self.assertEqual(response.status_code, 200)
        self.participant.refresh_from_db()
        self.assertIsNotNone(self.participant.joined_at)

    def test_reused_token_is_rejected(self):
        self.client.get(f"/registration/verify/{self.token.token}/")
        response = self.client.get(f"/registration/verify/{self.token.token}/")
        self.assertEqual(response.status_code, 410)

    def test_expired_token_is_rejected(self):
        self.token.expires_at = timezone.now() - timedelta(hours=1)
        self.token.save()
        response = self.client.get(f"/registration/verify/{self.token.token}/")
        self.assertEqual(response.status_code, 410)

    def test_invalid_token_returns_404(self):
        response = self.client.get("/registration/verify/nonexistent-token/")
        self.assertEqual(response.status_code, 404)
