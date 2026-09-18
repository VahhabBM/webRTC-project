import os

from .base import *  # noqa: F403
from .base import env_int, env_list

DEBUG = False
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")  # noqa: F405

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

COTURN_REGION1_DOMAIN = os.environ.get("COTURN_REGION1_DOMAIN", "staging-turn.yourdomain.com")
COTURN_REGION2_DOMAIN = os.environ.get("COTURN_REGION2_DOMAIN", "staging-turn-r2.yourdomain.com")
COTURN_PORT = env_int("COTURN_PORT", 3478)
COTURN_TLS_PORT = env_int("COTURN_TLS_PORT", 5349)
COTURN_REGION2_PORT = env_int("COTURN_REGION2_PORT", 3478)
COTURN_REGION2_TLS_PORT = env_int("COTURN_REGION2_TLS_PORT", 5349)
COTURN_SHARED_SECRET = os.environ.get(
    "COTURN_SHARED_SECRET", "super_secret_staging_turn_key_2026_very_long_entropy"
)
COTURN_REGION2_SHARED_SECRET = os.environ.get(
    "COTURN_REGION2_SHARED_SECRET", COTURN_SHARED_SECRET
)
COTURN_CREDENTIAL_TTL_SECONDS = env_int("COTURN_CREDENTIAL_TTL_SECONDS", 1800)
