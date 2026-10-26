from decimal import Decimal, InvalidOperation

from apps.events.countries import COUNTRY_CODES


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
        raise RegistrationError(
            "You must accept the terms and conditions.", "terms_not_accepted"
        )

    tag_selection_provided = "tag_ids" in data or "tags" in data
    tag_ids = data.get("tag_ids") or data.get("tags") or []
    if len(tag_ids) > 5:
        raise RegistrationError("Too many tags selected.", "invalid_tag_count")

    # Check unique tags rule if event requires it
    if event and getattr(event, "unique_tags", False) and tag_selection_provided:
        if len(tag_ids) != 1:
            raise RegistrationError(
                "Exactly one tag must be selected.", "invalid_tag_count"
            )

    age = data.get("age")
    if age is None or (isinstance(age, str) and not age.strip()):
        raise RegistrationError(
            "Age is required. Registration is only available to people aged 18 or older.",
            "age_required",
        )
    try:
        age_value = Decimal(str(age).strip())
        if not age_value.is_finite() or age_value != age_value.to_integral_value():
            raise InvalidOperation
        age = int(age_value)
    except (InvalidOperation, TypeError, ValueError):
        raise RegistrationError("Enter a valid age.", "invalid_age")
    if age < 18:
        raise RegistrationError(
            "Registration is only available to people aged 18 or older.",
            "age_restricted",
        )
    country = data.get("country", "")
    if country is None:
        country = ""
    if not isinstance(country, str):
        raise RegistrationError("Choose a valid country.", "invalid_country")
    country = country.strip().upper()
    if country and country not in COUNTRY_CODES:
        raise RegistrationError("Choose a valid country.", "invalid_country")

    gender = data.get("gender", "Other/Prefer Not to Say")
    want_to_date = data.get("want_to_date", "Any")
    whatsapp = data.get("whatsapp", "")

    return {
        "display_name": display_name,
        "age": age,
        "country": country,
        "gender": gender,
        "want_to_date": want_to_date,
        "email": email,
        "whatsapp": whatsapp,
        "tag_ids": tag_ids,
        "tags": tag_ids,
        "accepted_terms": accepted_terms,
    }
