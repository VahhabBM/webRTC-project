"""Administrator-triggered registration verification email delivery."""

from django.db import transaction
from django.utils import timezone

from apps.events.auth import event_end
from apps.events.models import EventStatus, Participant, RegistrationStatus
from apps.registration.models import EmailVerificationToken
from apps.registration.services.email_service import EmailService


@transaction.atomic
def resend_registration_verification_email(participant_id) -> bool:
    """Replace outstanding verification tokens and email a fresh one to a registrant."""
    participant = (
        Participant.objects.select_for_update()
        .select_related("event")
        .get(pk=participant_id)
    )
    now = timezone.now()
    if (
        participant.email_verified_at is not None
        or participant.registration_status
        not in {
            RegistrationStatus.PENDING,
            RegistrationStatus.CONFIRMED,
            RegistrationStatus.WAITLISTED,
        }
        or not participant.email
        or participant.event.status
        not in {
            EventStatus.REGISTRATION_OPEN,
            EventStatus.SCHEDULED,
            EventStatus.LOCKED,
            EventStatus.ACTIVE,
            EventStatus.RUNNING,
        }
        or event_end(participant) <= now
    ):
        return False

    participant.verification_tokens.filter(used_at__isnull=True).update(used_at=now)
    token = EmailVerificationToken.create_for(participant)
    EmailService.send_verification_email(
        recipient_email=participant.email,
        name=participant.display_name,
        token=token.token,
        event_title=participant.event.name,
        registration_status=participant.registration_status,
    )
    return True
