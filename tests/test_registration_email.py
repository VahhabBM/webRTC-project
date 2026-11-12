import json
from datetime import timedelta

from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Event, RegistrationStatus, Tag
from apps.registration.models import EmailVerificationToken, Participant


class RegistrationEmailTestCase(TestCase):
    def setUp(self):
        self.event = Event.objects.create(
            name="Tech Networking Event",
            num_rounds=3,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=timezone.now() + timedelta(days=1),
        )
        self.tag = Tag.objects.create(name="Backend")
        self.url = reverse("registration:register", args=[self.event.id])

    def test_registration_completes_without_verification_email_or_token(self):
        payload = {
            "display_name": "Test User",
            "email": "test@example.com",
            "email_confirmation": "test@example.com",
            "age": 29,
            "country": "NZ",
            "tag_ids": [str(self.tag.id)],
            "consents": {
                "age_confirmation": True,
                "sensitive_data_processing": True,
                "mutual_contact_sharing": True,
                "terms_and_privacy": True,
            },
        }

        # 1. Submit registration request
        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 201)

        participant = Participant.objects.get(email="test@example.com", event=self.event)
        self.assertEqual(participant.registration_status, RegistrationStatus.CONFIRMED)
        self.assertIsNone(participant.email_verified_at)
        self.assertIsNone(participant.joined_at)
        self.assertFalse(EmailVerificationToken.objects.filter(participant=participant).exists())
        self.assertEqual(mail.outbox, [])
        self.assertIn("confirmed", response.json()["message"].lower())
