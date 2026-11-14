from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.events.models import Participant, RegistrationStatus


def count_reserved_seats(
    event, *, gender=None, now=None, exclude_participant_id=None
) -> int:
    now = now or timezone.now()
    registrations = Participant.objects.filter(event=event).filter(
        Q(registration_status=RegistrationStatus.CONFIRMED)
        | Q(
            registration_status=RegistrationStatus.PENDING,
            reservation_expires_at__gt=now,
        )
    )
    if gender:
        registrations = registrations.filter(gender=gender)
    if exclude_participant_id is not None:
        registrations = registrations.exclude(pk=exclude_participant_id)
    return registrations.count()


def has_registration_capacity(
    event, *, gender=None, now=None, exclude_participant_id=None
) -> bool:
    if event is None:
        return True

    # Gender-specific registration cap check
    if gender == "Male" and event.male_registration_cap is not None:
        male_count = count_reserved_seats(
            event,
            gender="Male",
            now=now,
            exclude_participant_id=exclude_participant_id,
        )
        if male_count >= event.male_registration_cap:
            return False
    elif gender == "Female" and event.female_registration_cap is not None:
        female_count = count_reserved_seats(
            event,
            gender="Female",
            now=now,
            exclude_participant_id=exclude_participant_id,
        )
        if female_count >= event.female_registration_cap:
            return False

    capacity = max(0, int(getattr(settings, "EVENT_REGISTRATION_CAPACITY", 200)))
    return (
        count_reserved_seats(
            event, now=now, exclude_participant_id=exclude_participant_id
        )
        < capacity
    )
