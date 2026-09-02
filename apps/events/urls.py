from django.urls import path

from .views import current_participant, join_participant

urlpatterns = [
    path("join/<str:token>/", join_participant, name="participant-join"),
    path("participant/me/", current_participant, name="participant-me"),
]
