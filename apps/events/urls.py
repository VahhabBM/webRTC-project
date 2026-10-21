from django.urls import path

from .views import (
    TurnCredentialsAPIView,
    clock_sync_page,
    current_participant,
    ice_servers_view,
    join_participant,
    landing_page,
    submit_connection_report,
    submit_round_interest,
    video_room_page,
)

urlpatterns = [
    path("", landing_page, name="landing-page"),
    path("join/<str:token>/", join_participant, name="participant-join"),
    path("participant/me/", current_participant, name="participant-me"),
    path("clock-sync/", clock_sync_page, name="clock-sync"),
    path("room/", video_room_page, name="video-room"),
    path("api/ice-servers/", ice_servers_view, name="ice_servers"),
    path(
        "credentials/turn/", TurnCredentialsAPIView.as_view(), name="turn-credentials"
    ),
    path(
        "api/telemetry/connection/",
        submit_connection_report,
        name="submit-connection-report",
    ),
    path(
        "api/telemetry/connection/",
        submit_connection_report,
        name="submit-connection-report",
    ),
    path(
        "api/round-interest/",
        submit_round_interest,
        name="submit-round-interest",
    ),
]
