from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse


class EmailService:
    @staticmethod
    def send_verification_email(recipient_email, name, token, event_title="رویداد"):
        verify_url = f"{settings.FRONTEND_URL}{reverse('registration:verify-email', args=[token])}"

        subject = f"تأیید ثبت‌نام در رویداد: {event_title}"
        message = (
            f"سلام {name},\n\n"
            f"از ثبت‌نام شما در {event_title} سپاسگزاریم.\n"
            f"لطفاً برای فعال‌سازی حساب خود روی لینک زیر کلیک کنید:\n\n"
            f"{verify_url}\n\n"
            f"این لینک تا ۲۴ ساعت آینده معتبر است."
        )

        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [recipient_email],
                fail_silently=False,
            )
        except Exception as e:
            raise RuntimeError(f"خطا در ارسال ایمیل تأیید: {str(e)}")
