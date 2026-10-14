class RegistrationError(Exception):
    def __init__(self, message, code="invalid_data"):
        self.message = message
        self.code = code
        super().__init__(self.message)


def validate_registration_data(event, data):
    if event is not None and isinstance(event, dict) and data is None:
        data = event
        event = None

    if not isinstance(data, dict):
        raise RegistrationError("Invalid request data format.", "invalid_json")

    display_name = data.get("display_name", "").strip()
    if not display_name:
        raise RegistrationError("Full name is required.", "missing_full_name")

    email = data.get("email", "").strip()
    if not email or "@" not in email:
        raise RegistrationError("A valid email address is required.", "invalid_email")

    accepted_terms = data.get("accepted_terms", True)
    if not accepted_terms:
        raise RegistrationError("You must accept the terms and conditions.", "terms_not_accepted")

    tag_ids = data.get("tag_ids") or data.get("tags") or []
    if len(tag_ids) > 5:
        raise RegistrationError("Too many tags selected.", "invalid_tag_count")

    # Optional age validation if provided
    age = data.get("age")
    if age is not None:
        try:
            age = int(age)
            if age < 18 or age > 100:
                raise ValueError()
        except (TypeError, ValueError):
            raise RegistrationError("Valid age (at least 18) is required.", "invalid_age")

    gender = data.get("gender", "Other/Prefer Not to Say")
    want_to_date = data.get("want_to_date", "Any")
    whatsapp = data.get("whatsapp", "")

    return {
        "display_name": display_name,
        "age": age,
        "gender": gender,
        "want_to_date": want_to_date,
        "email": email,
        "whatsapp": whatsapp,
        "tag_ids": tag_ids,
        "accepted_terms": accepted_terms,
    }
