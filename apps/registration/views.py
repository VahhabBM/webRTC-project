import json

from django.http import JsonResponse
from django.shortcuts import render

from apps.registration.forms import RegistrationError, validate_registration_payload
from apps.registration.models import EmailVerificationToken, Participant
from apps.registration.services.email_service import EmailService


def register_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    try:
        data = json.loads(request.body)
        payload = validate_registration_payload(data)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request data."}, status=400)
    except RegistrationError as e:
        return JsonResponse({"error": e.message, "code": e.code}, status=400)

    # Check for duplicate email in the same context if needed
    email = payload["email"]
    if Participant.objects.filter(email=email).exists():
        return JsonResponse(
            {
                "error": "This email is already registered for this event. You can request a verification link resend."
            },
            status=400,
        )

    try:
        participant = Participant.objects.create(
            display_name=payload["display_name"],
            email=email,
        )
        token_obj = EmailVerificationToken.objects.create(participant=participant)

        # Send verification email
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

    if token_obj.is_expired or token_obj.is_used:
        if is_json_request:
            return JsonResponse(
                {"error": "This link has expired or has already been used."}, status=410
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

    token_obj.is_used = True
    token_obj.save()

    participant = token_obj.participant
    participant.is_active = True
    participant.save()

    if is_json_request:
        return JsonResponse(
            {"message": "Your registration has been successfully verified."}, status=200
        )

    return render(request, "registration/success.html", {"participant": participant})
