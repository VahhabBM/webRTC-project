from __future__ import annotations

import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.events.models import Event, Participant, ParticipantTag
from apps.registration.services.email_service import EmailService

from .forms import RegistrationError, validate_registration_data
from .models import EmailVerificationToken, TermsAcceptance
from .rate_limit import is_rate_limited, record_attempt

logger = logging.getLogger("registration")


@method_decorator(csrf_exempt, name="dispatch")
class RegisterView(View):
    def post(self, request, event_id):
        event = get_object_or_404(Event, id=event_id)

        client_ip = request.META.get("REMOTE_ADDR", "unknown")
        rate_key = f"register:{client_ip}"
        if is_rate_limited(rate_key):
            return JsonResponse(
                {"error": "تعداد درخواست‌های شما زیاد است. لطفاً کمی صبر کنید."},
                status=429,
            )
        record_attempt(rate_key)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "داده ارسالی نامعتبر است."}, status=400)

        try:
            clean_data = validate_registration_data(event, payload)
        except RegistrationError as exc:
            return JsonResponse({"error": exc.message, "code": exc.code}, status=400)

        with transaction.atomic():
            participant = Participant.objects.create(
                event=event,
                display_name=clean_data["display_name"],
                email=clean_data["email"],
            )
            ParticipantTag.objects.bulk_create(
                ParticipantTag(participant=participant, tag=tag)
                for tag in clean_data["tags"]
            )
            TermsAcceptance.objects.create(
                participant=participant, ip_address=client_ip
            )
            token = EmailVerificationToken.create_for(participant)

        self._send_verification_email(participant, token)

        logger.info(
            "participant registered",
            extra={"participant_id": str(participant.id), "event_id": str(event.id)},
        )
        return JsonResponse(
            {"message": "ثبت‌نام شما دریافت شد. لطفاً ایمیل خود را بررسی کنید."},
            status=201,
        )

    @staticmethod
    def _send_verification_email(participant, token):
        logger.info(
            "verification email queued",
            extra={"participant_id": str(participant.id), "token": token.token},
        )


class VerifyEmailView(View):
    def get(self, request, token):
        verification = EmailVerificationToken.objects.filter(token=token).first()

        if verification is None:
            return JsonResponse({"error": "لینک تأیید نامعتبر است."}, status=404)

        if not verification.is_valid():
            return JsonResponse(
                {"error": "این لینک منقضی شده یا قبلاً استفاده شده است."},
                status=410,
            )

        participant = verification.participant
        participant.joined_at = verification.used_at or None
        from django.utils import timezone

        participant.joined_at = timezone.now()
        participant.save(update_fields=["joined_at"])
        verification.mark_used()

        return JsonResponse({"message": "ثبت‌نام شما با موفقیت تأیید شد."}, status=200)


@staticmethod
def _send_verification_email(participant, token):
    event_title = getattr(participant.event, "title", "همسان‌گزینی")
    EmailService.send_verification_email(
        recipient_email=participant.email,
        name=participant.name,
        token=token.token,
        event_title=event_title,
    )
