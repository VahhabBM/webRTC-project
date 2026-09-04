from __future__ import annotations

import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.events.models import Event, Participant, ParticipantTag, Tag
from apps.registration.services.email_service import EmailService

from .forms import (
    MAX_TAGS,
    MIN_TAGS,
    RegistrationError,
    validate_registration_data,
)
from .models import DeviceCheckLog, EmailVerificationToken, TermsAcceptance
from .rate_limit import is_rate_limited, record_attempt

logger = logging.getLogger("registration")


@method_decorator(csrf_exempt, name="dispatch")
class RegisterView(View):
    def get(self, request, event_id):
        event = get_object_or_404(Event, id=event_id)
        tags = Tag.objects.all().order_by("name")
        return render(
            request,
            "registration/register.html",
            {
                "event": event,
                "tags": tags,
                "min_tags": MIN_TAGS,
                "max_tags": MAX_TAGS,
            },
        )

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
            extra={
                "participant_id": str(participant.id),
                "event_id": str(event.id),
            },
        )
        return JsonResponse(
            {
                "message": (
                    "ثبت‌نام شما با موفقیت دریافت شد. لطفاً ایمیل خود را بررسی کنید."
                ),
                "email": clean_data["email"],
            },
            status=201,
        )

    @staticmethod
    def _send_verification_email(participant, token):
        event_name = getattr(participant.event, "name", "Matching Event")
        EmailService.send_verification_email(
            recipient_email=participant.email,
            name=participant.display_name,
            token=token.token,
            event_title=event_name,
        )


class VerifyEmailView(View):
    def get(self, request, token):
        verification = EmailVerificationToken.objects.filter(token=token).first()
        is_json_request = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in request.headers.get("Accept", "")
            or request.GET.get("format") == "json"
        )

        if verification is None:
            if is_json_request:
                return JsonResponse({"error": "لینک تأیید نامعتبر است."}, status=404)
            return render(
                request,
                "registration/token_expired.html",
                {
                    "title": "Invalid Link",
                    "message": "This activation link was not found or has invalid structure.",
                },
                status=404,
            )

        if not verification.is_valid():
            if is_json_request:
                return JsonResponse(
                    {"error": "این لینک منقضی شده یا قبلاً استفاده شده است."},
                    status=410,
                )
            return render(
                request,
                "registration/token_expired.html",
                {
                    "title": "Link Expired",
                    "message": "The 24-hour expiration window has passed or this link was already used.",
                },
                status=410,
            )

        participant = verification.participant
        participant.joined_at = timezone.now()
        participant.save(update_fields=["joined_at"])
        verification.mark_used()

        if is_json_request:
            return JsonResponse(
                {"message": "ثبت‌نام شما با موفقیت تأیید شد."}, status=200
            )

        return render(
            request,
            "registration/verify_result.html",
            {
                "participant": participant,
                "event": participant.event,
            },
            status=200,
        )


class CheckEmailView(View):
    def get(self, request):
        email = request.GET.get("email", "")
        return render(request, "registration/check_email.html", {"email": email})


class DeviceCheckView(View):
    def get(self, request, event_id=None):
        event = get_object_or_404(Event, id=event_id) if event_id else None
        return render(
            request,
            "registration/device_check.html",
            {
                "event": event,
                "event_id": str(event_id) if event_id else "",
            },
        )


@method_decorator(csrf_exempt, name="dispatch")
class DeviceCheckReportView(View):
    def post(self, request, event_id=None):
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid payload."}, status=400)

        event = get_object_or_404(Event, id=event_id) if event_id else None
        client_ip = request.META.get("REMOTE_ADDR")
        user_agent = request.META.get("HTTP_USER_AGENT", "")

        log_entry = DeviceCheckLog.objects.create(
            event=event,
            camera_working=bool(payload.get("camera_working")),
            mic_working=bool(payload.get("mic_working")),
            error_type=str(payload.get("error_type", ""))[:64],
            user_agent=user_agent,
            is_in_app_browser=bool(payload.get("is_in_app_browser")),
            ip_address=client_ip,
        )

        logger.info(
            "Device check reported",
            extra={
                "log_id": str(log_entry.id),
                "camera": log_entry.camera_working,
                "mic": log_entry.mic_working,
                "error": log_entry.error_type,
            },
        )
        return JsonResponse({"status": "recorded", "id": str(log_entry.id)}, status=201)
