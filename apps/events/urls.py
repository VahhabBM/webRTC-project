from django.urls import path

from .views import (
    clock_sync_page,
    current_participant,
    ice_servers_view,
    join_participant,
    video_room_page,
)

urlpatterns = [
    path("join/<str:token>/", join_participant, name="participant-join"),
    path("participant/me/", current_participant, name="participant-me"),
    path("clock-sync/", clock_sync_page, name="clock-sync"),
    path("room/", video_room_page, name="video-room"),
    path("api/ice-servers/", ice_servers_view, name="ice_servers"),
]
