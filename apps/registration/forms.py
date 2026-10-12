MIN_TAGS = 1
MAX_TAGS = 5


class RegistrationError(Exception):
    """User-friendly error for display (not a technical error)."""

    def __init__(self, message, code="general"):
        super().__init__(message)
        self.message = message
        self.code = code


def validate_registration_data(data, event=None):
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

    # When unique_tags is enabled the event restricts each participant to
    # exactly one tag; otherwise the global MIN/MAX bounds apply.
    if event and getattr(event, "unique_tags", False):
        if len(tag_ids) != 1:
            raise RegistrationError(
                "You must select exactly one interest for this event.",
                code="invalid_tag_count",
            )
    else:
        if not (MIN_TAGS <= len(tag_ids) <= MAX_TAGS):
            raise RegistrationError(
                f"Please select between {MIN_TAGS} and {MAX_TAGS} interests.",
                code="invalid_tag_count",
            )

    return {
        "display_name": display_name,
        "email": email,
        "tag_ids": tag_ids,
        "terms_accepted": terms_accepted,
    }


# Alias for backwards compatibility
validate_registration_payload = validate_registration_data
