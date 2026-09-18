from __future__ import annotations

import json

from django.conf import settings
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.events.auth import (
    ExpiredJoinToken,
    InvalidJoinToken,
    authenticate_join_token,
)
from apps.events.models import ConnectionType, Pair, Participant
from apps.events.turn import TurnCredentialService, generate_ice_servers


def get_authenticated_participant(request: HttpRequest) -> Participant | None:
    """دریافت شرکت‌کننده احراز هویت شده از روی سشن."""
    participant_id = request.session.get("participant_id")
    if not participant_id:
        return None
    try:
        return Participant.objects.select_related("event").get(pk=participant_id)
    except Participant.DoesNotExist:
        return None


@require_GET
def join_participant(request: HttpRequest, token: str) -> JsonResponse:
    """احراز هویت شرکت‌کننده از طریق Join Token با تفکیک خطاهای 400 و 410."""
    try:
        participant = authenticate_join_token(token)
    except InvalidJoinToken:
        return JsonResponse(
            {
                "error": {
                    "code": "join_token_invalid",
                    "message": "Invalid join token",
                },
                "authenticated": False,
            },
            status=400,
        )
    except ExpiredJoinToken:
        return JsonResponse(
            {
                "error": {
                    "code": "join_token_expired",
                    "message": "Join token expired",
                },
                "authenticated": False,
            },
            status=410,
        )

    request.session["participant_id"] = str(participant.pk)
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


# Alias برای حفظ سازگاری
join_view = join_participant


@require_GET
def current_participant(request: HttpRequest) -> JsonResponse:
    """دریافت اطلاعات شرکت‌کننده جاری در سشن."""
    participant = get_authenticated_participant(request)
    if not participant:
        return JsonResponse(
            {"error": "Authentication required", "authenticated": False},
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


# Alias برای حفظ سازگاری
participant_me_view = current_participant


@require_GET
def clock_sync_page(request: HttpRequest) -> HttpResponse:
    """ویوی رندر صفحه همگام‌سازی ساعت."""
    participant = get_authenticated_participant(request)
    if not participant:
        return JsonResponse(
            {"error": "Authentication required", "authenticated": False},
            status=401,
        )
    return render(request, "events/clock_sync.html")


@require_GET
def video_room_page(request: HttpRequest) -> HttpResponse:
    """ویوی رندر اتاق گفت‌وگوی تصویری و استخراج آخرین جفت اختصاص‌یافته."""
    participant = get_authenticated_participant(request)
    if not participant:
        return JsonResponse(
            {"error": "Authentication required", "authenticated": False},
            status=401,
        )

    pair = (
        Pair.objects.filter(event=participant.event)
        .filter(Q(participant_a=participant) | Q(participant_b=participant))
        .select_related("participant_a", "participant_b", "round")
        .order_by("-round__number")
        .first()
    )

    room_id = ""
    partner_id = ""
    if pair:
        partner = (
            pair.participant_b
            if pair.participant_a_id == participant.pk
            else pair.participant_a
        )
        room_id = pair.room_id
        partner_id = str(partner.pk)

    return render(
        request,
        "events/video_room.html",
        {
            "room_id": room_id,
            "partner_id": partner_id,
            "warning_threshold_seconds": getattr(
                settings, "ORCHESTRATOR_FINAL_SECONDS", 30
            ),
        },
    )


# Alias برای حفظ سازگاری
video_room_view = video_room_page


@require_GET
def ice_servers_view(request: HttpRequest) -> JsonResponse:
    """اندپوینت دریافت کانفیگ RTCIceServer و کریدنPage موقت TURN (تسک T-28)."""
    participant = get_authenticated_participant(request)
    if not participant:
        return JsonResponse(
            {"error": "Authentication required", "code": "UNAUTHORIZED"},
            status=401,
        )

    try:
        requested_ttl = int(request.GET.get("ttl", 3600))
        ttl = min(max(requested_ttl, 60), 86400)
    except (ValueError, TypeError):
        ttl = 3600

    data = generate_ice_servers(participant_id=str(participant.pk), ttl=ttl)
    return JsonResponse(data)


class TurnCredentialsAPIView(APIView):
    """بازگرداندن اعتبارنامه‌های منقضاشونده اتصال به سرور کمکی TURN."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        participant = getattr(request.user, "participant_profile", None)
        participant_id = str(participant.id) if participant else str(request.user.id)

        service = TurnCredentialService()
        data = service.generate_credentials(participant_id=participant_id)

        return Response(data, status=status.HTTP_200_OK)


@csrf_exempt
@require_POST
def submit_connection_report(request: HttpRequest) -> JsonResponse:
    """
    ثبت نوع اتصال و زمان برقراری به میلی‌ثانیه برای هر طرف جفت (T-37).
    فاقد هرگونه توکن یا اطلاعات هویتی؛ اعتبارسنجی منحصراً از سشن شرکت‌کننده انجام می‌شود.
    """
    participant = get_authenticated_participant(request)
    if not participant:
        return JsonResponse(
            {
                "error": {
                    "code": "unauthorized",
                    "message": "Participant session required",
                }
            },
            status=401,
        )

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"error": {"code": "invalid_json", "message": "Malformed JSON payload"}},
            status=400,
        )

    room_id = data.get("room_id")
    conn_type = data.get("connection_type")
    conn_time_ms = data.get("connection_time_ms")

    if not room_id or conn_type not in ConnectionType.values:
        return JsonResponse(
            {
                "error": {
                    "code": "invalid_payload",
                    "message": "Valid room_id and connection_type (direct/relay) required",
                }
            },
            status=400,
        )

    if not isinstance(conn_time_ms, int) or conn_time_ms < 0:
        return JsonResponse(
            {
                "error": {
                    "code": "invalid_payload",
                    "message": "connection_time_ms must be a non-negative integer",
                }
            },
            status=400,
        )

    # به‌روزرسانی اتمیک بدون Lock Contention برای مقیاس‌پذیری راندهای سنگین (تا ۹۰۵ جفت)
    updated_a = Pair.objects.filter(
        room_id=room_id,
        participant_a=participant,
    ).update(
        connection_type_a=conn_type,
        connection_time_ms_a=conn_time_ms,
    )

    if not updated_a:
        updated_b = Pair.objects.filter(
            room_id=room_id,
            participant_b=participant,
        ).update(
            connection_type_b=conn_type,
            connection_time_ms_b=conn_time_ms,
        )
        if not updated_b:
            return JsonResponse(
                {
                    "error": {
                        "code": "pair_not_found",
                        "message": "No matching pair found for this room and participant",
                    }
                },
                status=404,
            )

    return JsonResponse({"status": "recorded"})
