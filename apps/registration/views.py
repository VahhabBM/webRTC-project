import json

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template import TemplateDoesNotExist
from django.utils import timezone
from django.views import View

from apps.events.models import Event, Tag
from apps.registration.forms import RegistrationError, validate_registration_data
from apps.registration.models import (
    DeviceCheckLog,
    EmailVerificationToken,
    Participant,
)
from apps.registration.services.email_service import EmailService


def is_rate_limited(request):
    """Rate limit helper."""
    return False


def _safe_render(request, template_names, context, status=200, fallback_html=""):
    """Tries multiple possible template names before falling back to inline HTML."""
    if isinstance(template_names, str):
        template_names = [template_names]
    for tpl in template_names:
        try:
            return render(request, tpl, context, status=status)
        except TemplateDoesNotExist:
            continue
    return HttpResponse(fallback_html, status=status)


def register_view(request, event_id=None):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    if is_rate_limited(request):
        return JsonResponse(
            {"error": "Too many requests. Please try again later."}, status=429
        )

    event = None
    if event_id:
        event = Event.objects.filter(id=event_id).first()

    try:
        data = json.loads(request.body)
        payload = (
            validate_registration_data(event, data)
            if event
            else validate_registration_data(data)
        )
    except json.JSONDecodeError:
        return JsonResponse(
            {"error": "Invalid request data.", "code": "invalid_json"}, status=400
        )
    except RegistrationError as e:
        return JsonResponse({"error": e.message, "code": e.code}, status=400)

    email = payload["email"]
    query = Participant.objects.filter(email=email)
    if event:
        query = query.filter(event=event)
    elif event_id:
        query = query.filter(event_id=event_id)

    if query.exists():
        return JsonResponse(
            {
                "error": "This email is already registered for this event.",
                "code": "duplicate_email",
            },
            status=400,
        )

    try:
        participant = Participant.objects.create(
            event=event,
            display_name=payload["display_name"],
            email=email,
        )
        tag_ids = payload.get("tag_ids") or payload.get("tags") or []
        if tag_ids and hasattr(participant, "tags"):
            tags = Tag.objects.filter(id__in=tag_ids)
            participant.tags.set(tags)

        if hasattr(EmailVerificationToken, "create_for"):
            token_obj = EmailVerificationToken.create_for(participant)
        else:
            token_obj = EmailVerificationToken.objects.create(
                participant=participant,
                expires_at=timezone.now() + timezone.timedelta(hours=24),
            )

        EmailService.send_verification_email(
            recipient_email=email,
            name=participant.display_name,
            token=token_obj.token,
        )
    except Exception as exc:
        return JsonResponse(
            {
                "error": f"Registration failed: {exc}",
                "code": "server_error",
            },
            status=500,
        )

    return JsonResponse(
        {
            "message": "Your registration has been successfully received. Please check your email.",
            "participant_id": str(participant.id),
        },
        status=201,
    )


def verify_email_view(request, token):
    is_json_request = request.headers.get(
        "Accept"
    ) == "application/json" or request.path.startswith("/api/")

    try:
        token_obj = EmailVerificationToken.objects.get(token=token)
    except EmailVerificationToken.DoesNotExist:
        if is_json_request:
            return JsonResponse({"error": "Invalid verification link."}, status=404)
        return _safe_render(
            request,
            [
                "registration/token_expired.html",
                "registration/verify_result.html",
                "registration/error.html",
            ],
            {
                "title": "Invalid Link",
                "message": "This link was not found in the system or has an invalid structure.",
                "success": False,
                "valid": False,
                "expired": True,
            },
            status=404,
            fallback_html="<!DOCTYPE html><html dir='ltr'><body><h1>Invalid or Expired Link</h1></body></html>",
        )

    now = timezone.now()
    is_expired = (
        token_obj.is_expired()
        if callable(getattr(token_obj, "is_expired", None))
        else getattr(
            token_obj,
            "is_expired",
            (token_obj.expires_at < now)
            if getattr(token_obj, "expires_at", None)
            else False,
        )
    )
    is_used = (
        bool(token_obj.used_at)
        if hasattr(token_obj, "used_at")
        else getattr(token_obj, "is_used", False)
    )

    if is_expired or is_used:
        if is_json_request:
            return JsonResponse(
                {"error": "This link has expired or has already been used."},
                status=410,
            )
        return _safe_render(
            request,
            [
                "registration/token_expired.html",
                "registration/verify_result.html",
                "registration/error.html",
            ],
            {
                "title": "Expired or Used Link",
                "message": "The validity period of this link has expired, or it has already been used.",
                "success": False,
                "valid": False,
                "expired": True,
            },
            status=410,
            fallback_html="<!DOCTYPE html><html dir='ltr'><body><h1>Expired or Used Link</h1></body></html>",
        )

    if hasattr(token_obj, "mark_used") and callable(token_obj.mark_used):
        token_obj.mark_used()
    else:
        if hasattr(token_obj, "used_at"):
            token_obj.used_at = now
        if hasattr(token_obj, "is_used"):
            token_obj.is_used = True
        token_obj.save()

    participant = token_obj.participant
    if participant:
        if hasattr(participant, "confirm") and callable(participant.confirm):
            participant.confirm()
        else:
            if hasattr(participant, "joined_at") and not participant.joined_at:
                participant.joined_at = now
            if hasattr(participant, "email_verified"):
                participant.email_verified = True
            if hasattr(participant, "is_verified"):
                participant.is_verified = True
            if hasattr(participant, "is_active"):
                participant.is_active = True
            if hasattr(participant, "status"):
                from apps.events.models import ParticipantStatus

                if hasattr(ParticipantStatus, "CONFIRMED"):
                    participant.status = ParticipantStatus.CONFIRMED
                elif hasattr(ParticipantStatus, "VERIFIED"):
                    participant.status = ParticipantStatus.VERIFIED
            participant.save()

    if is_json_request:
        return JsonResponse(
            {"message": "Your registration has been successfully verified."},
            status=200,
        )

    return _safe_render(
        request,
        [
            "registration/verify_result.html",
            "registration/verify_success.html",
            "registration/success.html",
        ],
        {
            "participant": participant,
            "event": getattr(participant, "event", None),
            "success": True,
            "valid": True,
        },
        status=200,
        fallback_html=f"<!DOCTYPE html><html dir='ltr'><body><h1>You are Confirmed!</h1><p>{participant.display_name if participant else ''}</p></body></html>",
    )


def check_email_view(request):
    """Renders the 'Check Your Inbox' page or returns JSON if requested via API."""
    email = request.GET.get("email", "").strip()
    is_json_request = request.headers.get(
        "Accept"
    ) == "application/json" or request.path.startswith("/api/")
    if is_json_request:
        if not email:
            return JsonResponse({"error": "Email parameter is required."}, status=400)
        exists = Participant.objects.filter(email=email).exists()
        return JsonResponse({"exists": exists}, status=200)

    return _safe_render(
        request,
        ["registration/check_email.html", "registration/check_inbox.html"],
        {"email": email},
        status=200,
        fallback_html=f"<!DOCTYPE html><html dir='ltr'><body><h1>Check Your Inbox</h1><p>{email}</p></body></html>",
    )


def device_check_view(request, event_id=None):
    """View to render the device check page."""
    event = Event.objects.filter(id=event_id).first() if event_id else None
    return _safe_render(
        request,
        ["registration/device_check.html"],
        {"event": event, "event_id": event_id},
        status=200,
    )


def device_check_report_view(request, event_id=None):
    """API endpoint to receive and record device check telemetry in DeviceCheckLog."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    event = Event.objects.filter(id=event_id).first() if event_id else None
    user_agent = request.META.get("HTTP_USER_AGENT", "")

    log_kwargs = {}
    model_fields = {f.name for f in DeviceCheckLog._meta.get_fields()}

    if "event" in model_fields and event:
        log_kwargs["event"] = event
    if "camera_working" in model_fields:
        log_kwargs["camera_working"] = bool(data.get("camera_working", False))
    if "mic_working" in model_fields:
        log_kwargs["mic_working"] = bool(data.get("mic_working", False))
    if "error_type" in model_fields:
        log_kwargs["error_type"] = data.get("error_type", "")
    if "is_in_app_browser" in model_fields:
        log_kwargs["is_in_app_browser"] = bool(data.get("is_in_app_browser", False))
    if "user_agent" in model_fields:
        log_kwargs["user_agent"] = user_agent

    DeviceCheckLog.objects.create(**log_kwargs)
    return JsonResponse({"status": "received"}, status=201)


class RegisterView(View):
    def get(self, request, event_id=None, *args, **kwargs):
        event = get_object_or_404(Event, id=event_id) if event_id else None
        tags = Tag.objects.all()
        return render(
            request,
            "registration/register.html",
            {"event": event, "event_id": event_id, "tags": tags},
        )

    def post(self, request, event_id=None, *args, **kwargs):
        return register_view(request, event_id=event_id)


class VerifyEmailView(View):
    def get(self, request, token, *args, **kwargs):
        return verify_email_view(request, token)


class CheckEmailView(View):
    def get(self, request, *args, **kwargs):
        return check_email_view(request)


class DeviceCheckView(View):
    def get(self, request, event_id=None, *args, **kwargs):
        return device_check_view(request, event_id=event_id)


class DeviceCheckReportView(View):
    def post(self, request, event_id=None, *args, **kwargs):
        return device_check_report_view(request, event_id=event_id)
