from django.urls import path

from .views import clock_sync_page, current_participant, join_participant

urlpatterns = [
    path("join/<str:token>/", join_participant, name="participant-join"),
    path("participant/me/", current_participant, name="participant-me"),
    path("clock-sync/", clock_sync_page, name="clock-sync"),
]
