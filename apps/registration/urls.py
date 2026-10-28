from django.urls import path

from .views import (
    CheckEmailView,
    DeviceCheckReportView,
    DeviceCheckView,
    LegalDocumentsView,
    RegisterView,
    VerifyEmailView,
)

app_name = "registration"

urlpatterns = [
    path("legal/", LegalDocumentsView.as_view(), name="legal"),
    path(
        "<uuid:event_id>/legal/",
        LegalDocumentsView.as_view(),
        name="legal-event",
    ),
    path("<uuid:event_id>/", RegisterView.as_view(), name="register-ui"),
    path(
        "events/<uuid:event_id>/register/",
        RegisterView.as_view(),
        name="register",
    ),
    path("check-email/", CheckEmailView.as_view(), name="check-email"),
    path("verify/<str:token>/", VerifyEmailView.as_view(), name="verify-email"),
    path(
        "device-check/",
        DeviceCheckView.as_view(),
        name="device-check-generic",
    ),
    path(
        "device-check/report/",
        DeviceCheckReportView.as_view(),
        name="device-check-report-generic",
    ),
    path(
        "<uuid:event_id>/device-check/",
        DeviceCheckView.as_view(),
        name="device-check",
    ),
    path(
        "<uuid:event_id>/device-check/report/",
        DeviceCheckReportView.as_view(),
        name="device-check-report",
    ),
]
