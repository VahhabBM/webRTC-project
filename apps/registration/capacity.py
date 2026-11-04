from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.events.models import Participant, RegistrationStatus


def count_reserved_seats(event, *, now=None, exclude_participant_id=None) -> int:
    now = now or timezone.now()
    registrations = Participant.objects.filter(event=event).filter(
        Q(registration_status=RegistrationStatus.CONFIRMED)
        | Q(
            registration_status=RegistrationStatus.PENDING,
            reservation_expires_at__gt=now,
        )
    )
    if exclude_participant_id is not None:
        registrations = registrations.exclude(pk=exclude_participant_id)
    return registrations.count()


def has_registration_capacity(event, *, now=None, exclude_participant_id=None) -> bool:
    capacity = max(0, int(getattr(settings, "EVENT_REGISTRATION_CAPACITY", 200)))
    return (
        count_reserved_seats(
            event, now=now, exclude_participant_id=exclude_participant_id
        )
        < capacity
    )
