from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

from apps.events.auth import personal_join_link


class EmailService:
    @staticmethod
    def send_registration_invitation_email(
        recipient_email, event_title, registration_url
    ):
        subject = f"You're invited to register for: {event_title}"
        message = (
            f"Hello,\n\n"
            f"You are invited to register for {event_title}.\n"
            "Complete your registration using the link below:\n\n"
            f"{registration_url}\n\n"
            "We look forward to seeing you at the event.\n\n"
            "Meetakk"
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
            raise RuntimeError(f"Error sending registration invitation: {str(e)}")

    @staticmethod
    def send_verification_email(
        recipient_email,
        name,
        token,
        event_title="Event",
        registration_status="pending",
    ):
        verify_url = f"{settings.FRONTEND_URL}{reverse('registration:verify-email', args=[token])}"

        subject = f"Registration Verification for: {event_title}"
        if registration_status == "waitlisted":
            registration_note = (
                "Your email verification is needed to keep your waitlist entry. "
                "A place is not confirmed unless you later accept an invitation."
            )
        else:
            registration_note = (
                "Your place is reserved for 24 hours. After you verify your email, "
                "your registration is complete and your personal event access link "
                "will be emailed 24 hours before the event starts."
            )

        message = (
            f"Hello {name},\n\n"
            f"Thank you for registering for {event_title}.\n"
            "Please click the link below to verify your email address and activate your registration.\n\n"
            f"{verify_url}\n\n"
            f"{registration_note}\n\n"
            "This verification link is valid for the next 24 hours."
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

    @staticmethod
    def send_waitlist_invitation_email(
        recipient_email, name, event_title, message
    ):
        subject = f"A place is available for: {event_title}"
        body = f"Hello {name},\n\n{message}"
        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient_email],
                fail_silently=False,
            )
        except Exception as e:
            raise RuntimeError(f"Error sending waitlist invitation: {str(e)}")
