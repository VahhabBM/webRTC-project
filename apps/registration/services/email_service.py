from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse


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
