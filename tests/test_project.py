from django.conf import settings
from django.test import Client


def test_django_project_loads():
    assert settings.configured
    assert settings.TIME_ZONE == "UTC"
    assert settings.LANGUAGE_CODE == "fa-ir"
    assert settings.USE_TZ is True


def test_intentional_failure_for_ci_verification():
    """Deliberate failure to verify CI goes red. Must be removed before merge."""
    assert False, "CI verification: this test must fail"


def test_health_endpoint():
    response = Client().get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
