from django.http import JsonResponse
from django.shortcuts import render
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


@require_GET
def clock_sync_page(request):
    if resolve_participant_from_session(request.session) is None:
        return JsonResponse({"error": {"code": "not_authenticated"}}, status=401)
    return render(request, "events/clock_sync.html")


@require_GET
def video_room_page(request):
    participant = resolve_participant_from_session(request.session)
    if participant is None:
        return JsonResponse({"error": {"code": "not_authenticated"}}, status=401)

    from django.db.models import Q

    from apps.events.models import Pair

    pair = (
        Pair.objects.filter(Q(participant_a=participant) | Q(participant_b=participant))
        .select_related("participant_a", "participant_b")
        .first()
    )

    partner = None
    room_id = None
    if pair:
        partner = (
            pair.participant_b
            if pair.participant_a_id == participant.pk
            else pair.participant_a
        )
        room_id = pair.room_id

    context = {
        "room_id": room_id,
        "partner_id": str(partner.pk) if partner else None,
        "partner_name": partner.display_name if partner else None,
    }
    return render(request, "events/video_room.html", context)
