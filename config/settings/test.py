"""Isolated test settings; never use these to run the application."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-not-a-real-secret")
os.environ.setdefault("POSTGRES_DB", "webrtc_test")
os.environ.setdefault("POSTGRES_USER", "webrtc_test")
os.environ.setdefault("POSTGRES_PASSWORD", "test-only")

from .base import *  # noqa: E402,F403

DEBUG = True
ALLOWED_HOSTS = ["localhost", "testserver"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
# The events app ships a PostgreSQL-only trigger migration. SQLite test runs
# create its models from the current schema instead; PostgreSQL migrations and
# trigger behavior are checked separately in the production database workflow.
MIGRATION_MODULES = {"events": None, "registration": None}
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
