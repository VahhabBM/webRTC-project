import json
from datetime import timedelta

from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Event, ParticipantStatus, Tag
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

    def test_email_is_sent_and_activates_account(self):
        payload = {
            "display_name": "Test User",
            "email": "test@example.com",
            "age": 29,
            "tag_ids": [str(self.tag.id)],
            "accepted_terms": True,
        }

        # 1. Submit registration request
        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 201)

        # 2. Verify email is created in outbox with English activation text
        self.assertEqual(len(mail.outbox), 1)
        email_body = mail.outbox[0].body
        self.assertIn("activate", email_body.lower())

        # 3. Verify token creation and account activation via verification URL
        participant = Participant.objects.get(
            email="test@example.com", event=self.event
        )
        token_obj = EmailVerificationToken.objects.get(participant=participant)

        verify_url = reverse("registration:verify-email", args=[token_obj.token])
        verify_response = self.client.get(verify_url)
        self.assertEqual(verify_response.status_code, 200)

        participant.refresh_from_db()
        self.assertIsNotNone(participant.joined_at)
        if hasattr(ParticipantStatus, "CONFIRMED"):
            self.assertEqual(participant.status, ParticipantStatus.CONFIRMED)
