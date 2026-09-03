from django.core import mail
from django.test import SimpleTestCase

from apps.registration.services.email_service import EmailService


class EmailServiceTests(SimpleTestCase):
    def test_verify_email_rendering_and_dispatch(self):
        """بررسی رندر شدن نسخه متنی و HTML قالب تایید ایمیل و راست‌چین بودن آن"""
        context = {
            "name": "علی",
            "event_title": "رویداد تست",
            "action_url": "http://localhost:8000/registration/verify/test-token-123/",
            "expiration_hours": 24,
        }

        EmailService._send_worker(
            subject="تست تایید ایمیل",
            template_name="emails/verify_email.html",
            context=context,
            recipient_email="user@example.com",
        )

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, "تست تایید ایمیل")
        self.assertIn("علی", sent.body)
        self.assertEqual(sent.to, ["user@example.com"])

        # بررسی وجود HTML و ساختار RTL
        html_parts = [
            content for content, mime in sent.alternatives if mime == "text/html"
        ]
        self.assertEqual(len(html_parts), 1)
        self.assertIn('dir="rtl"', html_parts[0])
        self.assertIn("test-token-123", html_parts[0])

    def test_magic_link_email_rendering(self):
        """بررسی رندر شدن قالب لینک ورود یکتا"""
        context = {
            "name": "سارا",
            "event_title": "وبینار شبکه",
            "action_url": "http://localhost:8000/auth/session/magic-token-456/",
        }

        EmailService._send_worker(
            subject="پیوند ورود",
            template_name="emails/magic_link.html",
            context=context,
            recipient_email="sara@example.com",
        )

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertIn("سارا", sent.body)
        self.assertIn("magic-token-456", sent.body)
