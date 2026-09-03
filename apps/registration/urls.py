from django.urls import path

from .views import RegisterView, VerifyEmailView

urlpatterns = [
    path("events/<uuid:event_id>/register/", RegisterView.as_view(), name="register"),
    path("verify/<str:token>/", VerifyEmailView.as_view(), name="verify-email"),
]
