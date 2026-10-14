MIN_TAGS = 1
MAX_TAGS = 5


class RegistrationError(Exception):
    """User-friendly error for display (not a technical error)."""

    def __init__(self, message, code="general"):
        super().__init__(message)
        self.message = message
        self.code = code


def validate_registration_data(arg1, arg2=None):
    """Supports both (data, event=None) and (event, data) signatures for tests."""
    if hasattr(arg1, "get") and callable(arg1.get):
        data = arg1
        event = arg2
    else:
        event = arg1
        data = arg2 if arg2 is not None else {}

    display_name = data.get("display_name", "").strip()
    email = data.get("email", "").strip()
    terms_accepted = data.get("terms_accepted", True)

    # Support both 'tags' and 'tag_ids' keys from different tests
    tags = data.get("tags", data.get("tag_ids", []))

    if not display_name:
        raise RegistrationError("Please enter your name.", code="missing_name")

    if not email or "@" not in email:
        raise RegistrationError(
            "Please enter a valid email address.", code="invalid_email"
        )

    # Validate unique_tags constraint if event is provided
    if event and getattr(event, "unique_tags", False):
        if len(tags) != 1:
            raise RegistrationError(
                "You must select exactly one interest for this event.",
                code="invalid_tag_count",
            )
    else:
        if not (MIN_TAGS <= len(tags) <= MAX_TAGS):
            raise RegistrationError(
                f"Please select between {MIN_TAGS} and {MAX_TAGS} interests.",
                code="invalid_tag_count",
            )

    if data.get("accepted_terms") is False:
        raise RegistrationError(
            "You must accept the terms and privacy policy to register.",
            code="terms_not_accepted",
        )

    return {
        "display_name": display_name,
        "email": email,
        "tags": tags,
        "tag_ids": tags,
        "accepted_terms": terms_accepted,
    }


# Backwards compatibility alias
validate_registration_payload = validate_registration_data
