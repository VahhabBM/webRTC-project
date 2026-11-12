from django.core import mail
from django.test import SimpleTestCase

from apps.registration.services.email_service import EmailService


class EmailServiceTests(SimpleTestCase):
    def test_event_access_email_dispatches_personal_link(self):
        EmailService.send_event_access_email(
            recipient_email="user@example.com",
            name="علی",
            event_title="رویداد تست",
            join_url="https://meetakk.example/join/private-token/",
        )

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, "Your access link for: رویداد تست")
        self.assertIn("private-token", sent.body)
        self.assertEqual(sent.to, ["user@example.com"])

    def test_waitlist_invitation_email_dispatches(self):
        EmailService.send_waitlist_invitation_email(
            recipient_email="sara@example.com",
            name="سارا",
            event_title="وبینار شبکه",
            message="A place is available. Accept the invitation to confirm your place.",
        )

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, "A place is available for: وبینار شبکه")
        self.assertIn("سارا", sent.body)
        self.assertIn("Accept the invitation", sent.body)
