from django.urls import path

from .views import CheckEmailView, RegisterView, VerifyEmailView

app_name = "registration"

urlpatterns = [
    path("<uuid:event_id>/", RegisterView.as_view(), name="register-ui"),
    path(
        "events/<uuid:event_id>/register/",
        RegisterView.as_view(),
        name="register",
    ),
    path("check-email/", CheckEmailView.as_view(), name="check-email"),
    path("verify/<str:token>/", VerifyEmailView.as_view(), name="verify-email"),
]
