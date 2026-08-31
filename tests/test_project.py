from django.conf import settings
from django.test import Client


def test_django_project_loads():
    assert settings.configured
    assert settings.TIME_ZONE == "UTC"
    assert settings.LANGUAGE_CODE == "fa-ir"
    assert settings.USE_TZ is True



def test_health_endpoint():
    response = Client().get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
