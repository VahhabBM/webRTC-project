from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .auth import (
    ExpiredJoinToken,
    InvalidJoinToken,
    authenticate_join_token,
    establish_participant_session,
    resolve_participant_from_session,
)


@require_GET
def join_participant(request, token: str):
    try:
        participant = authenticate_join_token(token)
    except ExpiredJoinToken:
        return JsonResponse(
            {
                "error": {
                    "code": "join_token_expired",
                    "message": "This join link has expired.",
                }
            },
            status=410,
        )
    except InvalidJoinToken:
        return JsonResponse(
            {
                "error": {
                    "code": "join_token_invalid",
                    "message": "This join link is invalid.",
                }
            },
            status=400,
        )
    establish_participant_session(request, participant)
    return JsonResponse(
        {
            "authenticated": True,
            "participant": {
                "id": str(participant.pk),
                "display_name": participant.display_name,
                "event_id": str(participant.event_id),
            },
        }
    )


@require_GET
def current_participant(request):
    participant = resolve_participant_from_session(request.session)
    if participant is None:
        return JsonResponse(
            {
                "error": {
                    "code": "not_authenticated",
                    "message": "Participant authentication required.",
                }
            },
            status=401,
        )
    return JsonResponse(
        {
            "authenticated": True,
            "participant": {
                "id": str(participant.pk),
                "display_name": participant.display_name,
                "event_id": str(participant.event_id),
            },
        }
    )
