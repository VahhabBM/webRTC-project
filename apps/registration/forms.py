from __future__ import annotations

from apps.events.models import Event, Participant, Tag

MIN_TAGS = 1
MAX_TAGS = 5


class RegistrationError(Exception):
    """خطای قابل‌فهم برای نمایش به کاربر (نه خطای فنی)."""

    def __init__(self, message: str, code: str = "invalid"):
        self.message = message
        self.code = code
        super().__init__(message)


def validate_registration_data(event: Event, data: dict) -> dict:
    display_name = (data.get("display_name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    tag_ids = data.get("tag_ids") or []
    accepted_terms = bool(data.get("accepted_terms"))

    if not display_name:
        raise RegistrationError("لطفاً نام خود را وارد کنید.", code="missing_name")

    if not email or "@" not in email:
        raise RegistrationError("لطفاً یک ایمیل معتبر وارد کنید.", code="invalid_email")

    if not accepted_terms:
        raise RegistrationError(
            "برای ثبت‌نام باید قوانین و حریم خصوصی را بپذیرید.",
            code="terms_not_accepted",
        )

    if not (MIN_TAGS <= len(tag_ids) <= MAX_TAGS):
        raise RegistrationError(
            f"لطفاً بین {MIN_TAGS} تا {MAX_TAGS} علاقه‌مندی انتخاب کنید.",
            code="invalid_tag_count",
        )

    tags = list(Tag.objects.filter(id__in=tag_ids))
    if len(tags) != len(set(tag_ids)):
        raise RegistrationError(
            "یک یا چند علاقه‌مندی انتخاب‌شده معتبر نیست.", code="invalid_tags"
        )

    existing = Participant.objects.filter(event=event, email=email).first()
    if existing:
        raise RegistrationError(
            "این ایمیل قبلاً برای این رویداد ثبت‌نام کرده است. "
            "می‌توانید درخواست ارسال مجدد لینک تأیید را بدهید.",
            code="duplicate_email",
        )

    return {
        "display_name": display_name,
        "email": email,
        "tags": tags,
    }
