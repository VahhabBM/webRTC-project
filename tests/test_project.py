import pytest
from django.conf import settings
from django.test import Client


def test_django_project_loads():
    assert settings.configured
    assert settings.TIME_ZONE == "UTC"
    assert settings.LANGUAGE_CODE == "fa-ir"
    assert settings.USE_TZ is True


@pytest.mark.django_db
def test_health_endpoint():
    """Smoke-test the health endpoint.

    The autouse _use_local_memory_cache fixture (conftest.py) replaces the
    Redis cache backend with LocMemCache for all pytest-style tests, so
    _check_redis() succeeds without a live Redis instance.
    The @pytest.mark.django_db mark grants DB access for _check_database().
    """
    response = Client().get("/health/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["database"] == "up"
    assert data["redis"] == "up"
