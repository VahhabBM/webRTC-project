from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from apps.events.countries import COUNTRY_CODES
from apps.registration.legal_documents import REQUIRED_CONSENTS


class RegistrationError(Exception):
    def __init__(self, message, code="invalid_data"):
        self.message = message
        self.code = code
        super().__init__(self.message)


def validate_registration_data(event, data=None):
    if event is not None and isinstance(event, dict) and data is None:
        data = event
        event = None

    if not isinstance(data, dict):
        raise RegistrationError("Invalid request data format.", "invalid_json")

    raw_display_name = data.get("display_name", "")
    if not isinstance(raw_display_name, str):
        raise RegistrationError("Enter a valid first name or nickname.", "invalid_display_name")
    display_name = raw_display_name.strip()
    if not display_name:
        raise RegistrationError("First or nickname is required.", "missing_full_name")
    if len(display_name) > 200:
        raise RegistrationError("Name must be 200 characters or fewer.", "invalid_display_name")

    raw_email = data.get("email", "")
    email = raw_email.strip().casefold() if isinstance(raw_email, str) else ""
    repeated_email = data.get(
        "email_confirmation", data.get("email_confirm", data.get("confirm_email", ""))
    )
    repeated_email = (
        repeated_email.strip().casefold()
        if isinstance(repeated_email, str)
        else ""
    )
    if not repeated_email:
        raise RegistrationError(
            "Please enter your email address again.", "email_confirmation_required"
        )
    if email != repeated_email:
        raise RegistrationError("The email addresses do not match.", "email_mismatch")
    try:
        validate_email(email)
    except ValidationError:
        raise RegistrationError("A valid email address is required.", "invalid_email")

    consents = data.get("consents")
    if not isinstance(consents, dict) or any(
        consents.get(consent_id) is not True for consent_id in REQUIRED_CONSENTS
    ):
        raise RegistrationError(
            "Please tick every required privacy and consent box to register.",
            "consents_required",
        )

    tag_selection_provided = "tag_ids" in data or "tags" in data
    tag_ids = data.get("tag_ids") or data.get("tags") or []
    if not isinstance(tag_ids, list):
        raise RegistrationError("Choose valid tags.", "invalid_tags")
    if len(tag_ids) > 5:
        raise RegistrationError("Too many tags selected.", "invalid_tag_count")
    try:
        tag_ids = [str(UUID(str(tag_id))) for tag_id in tag_ids]
    except (TypeError, ValueError, AttributeError):
        raise RegistrationError("Choose valid tags.", "invalid_tags")

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
    if not country or country not in COUNTRY_CODES:
        raise RegistrationError("Choose a valid country.", "invalid_country")
    city = data.get("city", "")
    if not isinstance(city, str):
        raise RegistrationError("Enter a valid city.", "invalid_city")
    city = city.strip()
    if len(city) > 120:
        raise RegistrationError("City must be 120 characters or fewer.", "invalid_city")

    gender = data.get("gender", "Other/Prefer Not to Say")
    if gender not in {"Male", "Female", "Other/Prefer Not to Say"}:
        raise RegistrationError("Choose a valid gender option.", "invalid_gender")

    want_to_date = data.get("want_to_date", "Any")
    if want_to_date not in {"Male", "Female", "Any"}:
        raise RegistrationError(
            "Choose a valid date preference.", "invalid_date_preference"
        )

    whatsapp = data.get("whatsapp", "")

    return {
        "display_name": display_name,
        "age": age,
        "country": country,
        "city": city,
        "gender": gender,
        "want_to_date": want_to_date,
        "email": email,
        "whatsapp": whatsapp,
        "tag_ids": tag_ids,
        "tags": tag_ids,
        "consents": {consent_id: True for consent_id in REQUIRED_CONSENTS},
    }
