import pytest


@pytest.fixture(autouse=True)
def _use_local_memory_cache(settings):
    """Keep unit tests independent of a running Redis instance."""
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
