import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger("email_service")
_executor = ThreadPoolExecutor(max_workers=3)


class EmailService:
    @classmethod
    def send_verification_email(
        cls, recipient_email: str, name: str, token: str, event_title: str
    ):
        """ارسال ایمیل تایید ثبت‌نام بدون مسدود کردن پاسخ وب‌سرور"""
        action_url = f"{settings.FRONTEND_URL}/registration/verify/{token}/"
        context = {
            "name": name,
            "event_title": event_title,
            "action_url": action_url,
            "expiration_hours": 24,
        }
        subject = f"تأیید ایمیل برای حضور در رویداد {event_title}"
        cls._send_email_async(
            subject=subject,
            template_name="emails/verify_email.html",
            context=context,
            recipient_email=recipient_email,
        )

    @classmethod
    def send_magic_link_email(
        cls, recipient_email: str, name: str, token: str, event_title: str
    ):
        """ارسال لینک ورود یکتا (تسک 08)"""
        action_url = f"{settings.FRONTEND_URL}/auth/session/{token}/"
        context = {
            "name": name,
            "event_title": event_title,
            "action_url": action_url,
        }
        subject = f"پیوند ورود به رویداد {event_title}"
        cls._send_email_async(
            subject=subject,
            template_name="emails/magic_link.html",
            context=context,
            recipient_email=recipient_email,
        )

    @classmethod
    def _send_email_async(
        cls, subject: str, template_name: str, context: dict, recipient_email: str
    ):
        _executor.submit(
            cls._send_worker,
            subject=subject,
            template_name=template_name,
            context=context,
            recipient_email=recipient_email,
        )

    @staticmethod
    def _send_worker(
        subject: str, template_name: str, context: dict, recipient_email: str
    ):
        try:
            html_content = render_to_string(template_name, context)
            text_content = strip_tags(html_content)

            msg = EmailMultiAlternatives(
                subject=subject,
                body=text_content,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[recipient_email],
            )
            msg.attach_alternative(html_content, "text/html")
            msg.send(fail_silently=False)

            logger.info("Email dispatched successfully", extra={"subject": subject})
        except Exception as exc:
            logger.error("Failed to send email: %s", str(exc))
