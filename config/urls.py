from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", include("apps.health.urls")),
    path("", include("apps.events.urls")),
    path("registration/", include("apps.registration.urls")),
]
