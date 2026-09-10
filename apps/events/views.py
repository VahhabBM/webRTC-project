from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.events.auth import verify_join_token
from apps.events.models import Pair, Participant
from apps.events.turn import generate_ice_servers


def get_authenticated_participant(request: HttpRequest) -> Participant | None:
    """دریافت شرکت‌کننده احراز هویت شده از روی سشن"""
    participant_id = request.session.get("participant_id")
    if not participant_id:
        return None
    try:
        return Participant.objects.select_related("event").get(pk=participant_id)
    except Participant.DoesNotExist:
        return None


@require_GET
def join_participant(request: HttpRequest, token: str) -> JsonResponse:
    """احراز هویت شرکت‌کننده از طریق Join Token و تنظیم نشست کاربر"""
    participant = verify_join_token(token)
    if not participant:
        return JsonResponse(
            {"error": "Invalid or expired join token", "authenticated": False},
            status=401,
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
    """دریافت اطلاعات شرکت‌کننده جاری در سشن"""
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
    """ویوی رندر صفحه همگام‌سازی ساعت"""
    participant = get_authenticated_participant(request)
    if not participant:
        return JsonResponse(
            {"error": "Authentication required", "authenticated": False},
            status=401,
        )
    return render(request, "events/clock_sync.html")


@require_GET
def video_room_page(request: HttpRequest) -> HttpResponse:
    """ویوی رندر اتاق گفت‌وگوی تصویری و استخراج آخرین جفت اختصاص‌یافته"""
    participant = get_authenticated_participant(request)
    if not participant:
        return JsonResponse(
            {"error": "Authentication required", "authenticated": False},
            status=401,
        )

    # پیدا کردن آخرین جفت فعال کاربر در رویداد
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
        },
    )


# Alias برای حفظ سازگاری
video_room_view = video_room_page


@require_GET
def ice_servers_view(request: HttpRequest) -> JsonResponse:
    """اندپوینت دریافت کانفیگ RTCIceServer و کریدنشال موقت TURN (تسک T-28)"""
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
