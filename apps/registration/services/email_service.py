from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

from apps.events.auth import personal_join_link


class EmailService:
    @staticmethod
    def send_verification_email(recipient_email, name, token, event_title="Event"):
        verify_url = f"{settings.FRONTEND_URL}{reverse('registration:verify-email', args=[token])}"

        subject = f"Registration Verification for: {event_title}"
        message = (
            f"Hello {name},\n\n"
            f"Thank you for registering for {event_title}.\n"
            f"Please click the link below to activate your account:\n\n"
            f"{verify_url}\n\n"
            f"This link is valid for the next 24 hours."
        )

        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient_email],
                fail_silently=False,
            )
        except Exception as e:
            raise RuntimeError(f"Error sending verification email: {str(e)}")

    @staticmethod
    def build_event_access_url(participant, raw_token):
        join_url = personal_join_link(participant, raw_token)
        if join_url.startswith(("http://", "https://")):
            return join_url
        return f"{settings.FRONTEND_URL.rstrip('/')}{join_url}"

    @staticmethod
    def send_event_access_email(recipient_email, name, event_title, join_url):
        subject = f"Your access link for: {event_title}"
        message = (
            f"Hello {name},\n\n"
            f"Your registration for {event_title} is verified. Use your personal "
            f"link below to enter the event room:\n\n"
            f"{join_url}\n\n"
            "This link is personal and expires when the event ends. "
            "Please do not forward it."
        )

        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient_email],
                fail_silently=False,
            )
        except Exception as e:
            raise RuntimeError(f"Error sending event access email: {str(e)}")
