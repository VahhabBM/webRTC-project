import hashlib
import json
import re

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template import TemplateDoesNotExist
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.decorators.http import require_http_methods

from apps.events.auth import event_end
from apps.events.countries import COUNTRY_CHOICES
from apps.events.models import Event, RegistrationStatus, Tag, WaitlistInvitation
from apps.registration.capacity import has_registration_capacity
from apps.registration.forms import RegistrationError, validate_registration_data
from apps.registration.legal_documents import (
    LAST_UPDATED,
    PRIVACY_POLICY,
    PRIVACY_VERSION,
    TERMS_AND_CONDITIONS,
    TERMS_VERSION,
)
from apps.registration.models import (
    DeviceCheckLog,
    EmailVerificationToken,
    Participant,
    TermsAcceptance,
)
from apps.registration.services.email_service import EmailService

_REGISTRATION_TITLE_DATE_SUFFIX = re.compile(
    r"\s*(?:[-–—|:]\s*)?(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\s*$",
    re.IGNORECASE,
)


def _registration_event_title(event):
    if event is None:
        return ""
    return _REGISTRATION_TITLE_DATE_SUFFIX.sub("", event.name).strip()


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

    event = Event.objects.filter(id=event_id).first() if event_id else None
    if event_id and event is None:
        return JsonResponse({"error": "Event not found."}, status=404)

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
    try:
        with transaction.atomic():
            if event:
                event = Event.objects.select_for_update().get(pk=event.pk)
            query = Participant.objects.filter(email=email)
            if event:
                query = query.filter(event=event)
            if query.exists():
                return JsonResponse(
                    {
                        "error": "This email is already registered for this event.",
                        "code": "duplicate_email",
                    },
                    status=400,
                )

            now = timezone.now()
            has_capacity = not event or has_registration_capacity(event, now=now)
            registration_status = (
                RegistrationStatus.PENDING
                if has_capacity
                else RegistrationStatus.WAITLISTED
            )
            reservation_expires_at = (
                now
                + timezone.timedelta(
                    hours=getattr(settings, "EMAIL_VERIFICATION_TTL_HOURS", 24)
                )
                if registration_status == RegistrationStatus.PENDING and event
                else None
            )
            participant = Participant.objects.create(
                event=event,
                display_name=payload["display_name"],
                email=email,
                age=payload.get("age"),
                country=payload.get("country", ""),
                gender=payload.get("gender", "Other/Prefer Not to Say"),
                want_to_date=payload.get("want_to_date", "Any"),
                whatsapp=payload.get("whatsapp", ""),
                registration_status=registration_status,
                reservation_expires_at=reservation_expires_at,
            )
            TermsAcceptance.objects.create(
                participant=participant,
                terms_version=TERMS_VERSION,
                privacy_version=PRIVACY_VERSION,
                consents=payload["consents"],
            )
            tag_ids = payload.get("tag_ids") or payload.get("tags") or []
            if tag_ids:
                participant.tags.set(Tag.objects.filter(id__in=tag_ids))

            if hasattr(EmailVerificationToken, "create_for"):
                token_obj = EmailVerificationToken.create_for(participant)
            else:
                token_obj = EmailVerificationToken.objects.create(
                    participant=participant,
                    expires_at=now + timezone.timedelta(hours=24),
                )

        EmailService.send_verification_email(
            recipient_email=email,
            name=participant.display_name,
            token=token_obj.token,
            event_title=participant.event.name,
            registration_status=participant.registration_status,
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
            "message": (
                "You are on the event waitlist. Please verify your email; a place "
                "is not confirmed until you accept an invitation."
                if participant.registration_status == RegistrationStatus.WAITLISTED
                else "Your place is reserved for 24 hours. Please verify your email to confirm it."
            ),
            "participant_id": str(participant.id),
            "registration_status": participant.registration_status,
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

    participant = token_obj.participant
    access_link_email_scheduled = False
    if participant:
        with transaction.atomic():
            event = Event.objects.select_for_update().get(pk=participant.event_id)
            locked_token = EmailVerificationToken.objects.select_for_update().get(
                pk=token_obj.pk
            )
            participant = Participant.objects.select_for_update().get(pk=participant.pk)
            now = timezone.now()
            if locked_token.used_at or locked_token.expires_at <= now:
                return JsonResponse(
                    {"error": "This link has expired or has already been used."},
                    status=410,
                ) if is_json_request else _safe_render(
                    request,
                    ["registration/token_expired.html", "registration/verify_result.html"],
                    {
                        "title": "Expired or Used Link",
                        "message": "This verification link has already been used.",
                        "success": False,
                        "valid": False,
                        "expired": True,
                    },
                    status=410,
                )
            locked_token.used_at = now
            locked_token.save(update_fields=["used_at"])
            participant.email_verified_at = participant.email_verified_at or now
            participant.joined_at = participant.joined_at or now
            if participant.registration_status == RegistrationStatus.PENDING:
                has_active_reservation = bool(
                    participant.reservation_expires_at
                    and participant.reservation_expires_at > now
                )
                if (
                    not has_active_reservation
                    and not has_registration_capacity(
                        event,
                        now=now,
                        exclude_participant_id=participant.pk,
                    )
                ):
                    participant.registration_status = RegistrationStatus.WAITLISTED
                    participant.reservation_expires_at = None
                else:
                    participant.registration_status = RegistrationStatus.CONFIRMED
                    participant.reservation_expires_at = None
            if hasattr(participant, "email_verified"):
                participant.email_verified = True
            if hasattr(participant, "is_verified"):
                participant.is_verified = True
            if hasattr(participant, "is_active"):
                participant.is_active = True
            participant.save()

        access_link_email_scheduled = bool(
            participant.registration_status == RegistrationStatus.CONFIRMED
            and participant.email_verified_at
            and participant.email
            and event_end(participant) > timezone.now()
        )

    registration_status = (
        participant.registration_status if participant else RegistrationStatus.PENDING
    )
    if registration_status == RegistrationStatus.WAITLISTED:
        verification_message = (
            "Your email is verified, and you are on the event waitlist. "
            "Your place is not confirmed unless you accept an invitation."
        )
    else:
        verification_message = (
            "Your email is verified and your registration is complete. "
            "Your event access link will be emailed 24 hours before the event starts."
        )

    if is_json_request:
        return JsonResponse(
            {
                "message": verification_message,
                "access_link_email_scheduled": access_link_email_scheduled,
                "registration_status": registration_status,
            },
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
            "registration_status": registration_status,
            "verification_message": verification_message,
            "success": True,
            "valid": True,
            "access_link_email_scheduled": access_link_email_scheduled,
        },
        status=200,
        fallback_html=(
            "<!DOCTYPE html><html dir='ltr'><body><h1>You are Confirmed!</h1>"
            f"<p>{participant.display_name if participant else ''}</p>"
            "<p>Your event access link will be emailed 24 hours before the event starts.</p>"
            "</body></html>"
        ),
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


@require_http_methods(["GET", "POST"])
def accept_waitlist_invitation(request, token):
    """Show a private invitation page; consume its one-time token only on POST."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    invitation_stub = get_object_or_404(
        WaitlistInvitation.objects.only("id", "event_id"), token_digest=digest
    )
    now = timezone.now()

    def invitation_response(invitation, participant, *, status=200, accepted=False):
        return render(
            request,
            "registration/waitlist_invitation.html",
            {
                "event": invitation.event,
                "participant": participant,
                "accepted": accepted,
                "can_accept": (
                    status == 200
                    and invitation.accepted_at is None
                    and invitation.revoked_at is None
                    and invitation.expires_at > now
                ),
                "message": (
                    "Your place is confirmed. Your event access link will arrive by email 24 hours before the event."
                    if accepted
                    else "This invitation is no longer valid or has already been used."
                    if status == 410
                    else "There is no available place right now. Your waitlist status has not changed."
                    if status == 409
                    else "Accept this invitation to confirm your place at the event."
                ),
            },
            status=status,
        )

    if request.method == "GET":
        invitation = get_object_or_404(
            WaitlistInvitation.objects.select_related("event", "participant"),
            pk=invitation_stub.pk,
        )
        invalid = (
            invitation.accepted_at is not None
            or invitation.revoked_at is not None
            or invitation.expires_at <= now
        )
        return invitation_response(
            invitation,
            invitation.participant,
            status=410 if invalid else 200,
        )

    with transaction.atomic():
        event = Event.objects.select_for_update().get(pk=invitation_stub.event_id)
        invitation = (
            WaitlistInvitation.objects.select_for_update()
            .filter(pk=invitation_stub.pk, event=event)
            .first()
        )
        if invitation is None:
            return JsonResponse({"error": "Invitation is no longer available."}, status=410)
        participant = Participant.objects.select_for_update().get(
            pk=invitation.participant_id
        )
        if (
            invitation.accepted_at is not None
            or invitation.revoked_at is not None
            or invitation.expires_at <= now
            or participant.registration_status != RegistrationStatus.WAITLISTED
            or participant.gender != "Female"
            or participant.email_verified_at is None
        ):
            return invitation_response(invitation, participant, status=410)
        if not has_registration_capacity(
            event, now=now, exclude_participant_id=participant.pk
        ):
            return invitation_response(invitation, participant, status=409)

        participant.registration_status = RegistrationStatus.CONFIRMED
        participant.reservation_expires_at = None
        participant.save(
            update_fields=["registration_status", "reservation_expires_at", "updated_at"]
        )
        invitation.accepted_at = now
        invitation.save(update_fields=["accepted_at"])

    return invitation_response(invitation, participant, accepted=True)


def device_check_view(request, event_id=None):
    """View to render the device check page."""
    event = Event.objects.filter(id=event_id).first() if event_id else None
    return _safe_render(
        request,
        ["registration/device_check.html"],
        {"event": event, "event_id": event_id},
        status=200,
    )


def legal_documents_view(request, event_id=None):
    event = get_object_or_404(Event, id=event_id) if event_id else None
    registration_url = (
        reverse("registration:register-ui", kwargs={"event_id": event_id})
        if event_id
        else "/"
    )
    return render(
        request,
        "registration/legal_documents.html",
        {
            "event": event,
            "registration_url": registration_url,
            "terms": TERMS_AND_CONDITIONS,
            "privacy": PRIVACY_POLICY,
            "last_updated": LAST_UPDATED,
        },
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
            {
                "event": event,
                "event_title": _registration_event_title(event),
                "event_id": event_id,
                "tags": tags,
                "country_choices": COUNTRY_CHOICES,
                "legal_url": (
                    reverse("registration:legal-event", kwargs={"event_id": event_id})
                    if event_id
                    else reverse("registration:legal")
                ),
            },
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


class LegalDocumentsView(View):
    def get(self, request, event_id=None, *args, **kwargs):
        return legal_documents_view(request, event_id=event_id)


class DeviceCheckReportView(View):
    def post(self, request, event_id=None, *args, **kwargs):
        return device_check_report_view(request, event_id=event_id)
