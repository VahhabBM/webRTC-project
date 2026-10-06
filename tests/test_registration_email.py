from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Event, Tag
from apps.registration.models import EmailVerificationToken


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RegistrationEmailTestCase(TestCase):
    def setUp(self):
        # مقداردهی کامل فیلدهای اجباری مدل Event شامل start_time
        self.event = Event.objects.create(
            name="رویداد تست ایمیل",
            num_rounds=3,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=timezone.now() + timedelta(days=1),
        )
        self.tag = Tag.objects.create(name="پایتون")
        self.url = reverse("registration:register", kwargs={"event_id": self.event.id})

    def test_email_is_sent_and_activates_account(self):
        payload = {
            "display_name": "کاربر تستی",
            "email": "test@example.com",
            "tag_ids": [str(self.tag.id)],
            "accepted_terms": True,
        }

        # ۱. ارسال درخواست ثبت‌نام
        response = self.client.post(
            self.url,
            data=payload,
            content_type="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 201)

        # ۲. بررسی اینکه ایمیل در حافظه ساخته شده است و متن آن معتبر است
        self.assertEqual(len(mail.outbox), 1)
        email_body = mail.outbox[0].body
        self.assertIn("فعال‌سازی", email_body)

        # ۳. استخراج توکن و لینک فعال‌سازی
        token_obj = EmailVerificationToken.objects.filter(
            participant__email="test@example.com"
        ).first()
        self.assertIsNotNone(token_obj)

        verify_url = reverse(
            "registration:verify-email", kwargs={"token": token_obj.token}
        )

        # ۴. باز کردن لینک فعال‌سازی در تست
        verify_response = self.client.get(verify_url)
        self.assertEqual(verify_response.status_code, 200)

        # ۵. بررسی فعال شدن حساب
        token_obj.participant.refresh_from_db()
        self.assertIsNotNone(token_obj.participant.joined_at)
