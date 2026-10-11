MIN_TAGS = 1
MAX_TAGS = 5


class RegistrationError(Exception):
    """User-friendly error for display (not a technical error)."""

    def __init__(self, message, code="general"):
        super().__init__(message)
        self.message = message
        self.code = code


def validate_registration_payload(data):
    display_name = data.get("display_name", "").strip()
    email = data.get("email", "").strip()
    terms_accepted = data.get("terms_accepted", False)
    tag_ids = data.get("tag_ids", [])

    if not display_name:
        raise RegistrationError("Please enter your name.", code="missing_name")

    if not email or "@" not in email:
        raise RegistrationError(
            "Please enter a valid email address.", code="invalid_email"
        )

    if not terms_accepted:
        raise RegistrationError(
            "You must accept the terms and privacy policy to register.",
            code="terms_not_accepted",
        )

    if not (MIN_TAGS <= len(tag_ids) <= MAX_TAGS):
        raise RegistrationError(
            f"Please select between {MIN_TAGS} and {MAX_TAGS} interests.",
            code="invalid_tag_count",
        )

    return {
        "display_name": display_name,
        "email": email,
        "tag_ids": tag_ids,
    }
