"""Production integrations on a private loopback-only rehearsal deployment.

Never select this module for the public website: local HTTP is deliberately
enabled for browser automation through an SSH tunnel. Use isolated databases
and synthetic participants only.
"""

from .production import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
