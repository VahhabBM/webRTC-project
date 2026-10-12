import json

from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View

from apps.registration.forms import RegistrationError, validate_registration_data
from apps.registration.models import EmailVerificationToken, Participant
from apps.registration.services.email_service import EmailService


def is_rate_limited(request):
    """Stub or rate limit helper to satisfy view requirements."""
    return False


def register_view(request, event_id=None):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    if is_rate_limited(request):
        return JsonResponse(
            {"error": "Too many requests. Please try again later."}, status=429
        )

    try:
        data = json.loads(request.body)
        payload = validate_registration_data(data)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request data."}, status=400)
    except RegistrationError as e:
        return JsonResponse({"error": e.message, "code": e.code}, status=400)

    email = payload["email"]
    query = Participant.objects.filter(email=email)
    if event_id:
        query = query.filter(event_id=event_id)
    if query.exists():
        return JsonResponse(
            {
                "error": "This email is already registered for this event. You can request a verification link resend."
            },
            status=400,
        )

    try:
        participant = Participant.objects.create(
            event_id=event_id if event_id else None,
            display_name=payload["display_name"],
            email=email,
        )
        token_obj = EmailVerificationToken.objects.create(participant=participant)

        EmailService.send_verification_email(
            recipient_email=email,
            name=participant.display_name,
            token=token_obj.token,
        )
    except Exception:
        return JsonResponse(
            {
                "error": "Registration completed, but failed to send the verification email. Please try again later."
            },
            status=500,
        )

    return JsonResponse(
        {
            "message": "Your registration has been successfully received. Please check your email."
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
        return render(
            request,
            "registration/error.html",
            {
                "title": "Invalid Link",
                "message": "This link was not found in the system or has an invalid structure.",
            },
            status=404,
        )

    is_expired = getattr(
        token_obj,
        "is_expired",
        token_obj.expires_at < timezone.now()
        if hasattr(token_obj, "expires_at")
        else False,
    )
    is_used = getattr(token_obj, "is_used", False)

    if is_expired or is_used:
        if is_json_request:
            return JsonResponse(
                {"error": "This link has expired or has already been used."},
                status=410,
            )
        return render(
            request,
            "registration/error.html",
            {
                "title": "Expired or Used Link",
                "message": "The 24-hour validity period of this link has expired, or you have already activated your account with it.",
            },
            status=410,
        )

    if hasattr(token_obj, "is_used"):
        token_obj.is_used = True
        token_obj.save()

    participant = token_obj.participant
    if participant:
        participant.is_active = True
        participant.save()

    if is_json_request:
        return JsonResponse(
            {"message": "Your registration has been successfully verified."},
            status=200,
        )

    return render(request, "registration/success.html", {"participant": participant})


def check_email_view(request):
    """API endpoint to check if an email is already registered."""
    email = request.GET.get("email", "").strip()
    if not email:
        return JsonResponse({"error": "Email parameter is required."}, status=400)

    exists = Participant.objects.filter(email=email).exists()
    return JsonResponse({"exists": exists}, status=200)


def device_check_view(request, event_id=None):
    """View to render the device check page."""
    return render(request, "registration/device_check.html", {"event_id": event_id})


def device_check_report_view(request, event_id=None):
    """API endpoint to receive device check telemetry."""
    return JsonResponse({"status": "received"}, status=201)


class RegisterView(View):
    def get(self, request, event_id=None, *args, **kwargs):
        return render(request, "registration/register.html", {"event_id": event_id})

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
