import os

from .base import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")  # noqa: F405

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", default=True)  # noqa: F405
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# T-49: production TURN relay. Address and shared credentials come from the
# environment only; no real secret is ever committed to this repository.
COTURN_REGION1_DOMAIN = os.environ.get("COTURN_REGION1_DOMAIN", "turn.meetakk.com")
COTURN_PORT = env_int("COTURN_PORT", 3478)  # noqa: F405
COTURN_TLS_PORT = env_int("COTURN_TLS_PORT", 5349)  # noqa: F405
COTURN_SHARED_SECRET = os.environ.get("COTURN_SHARED_SECRET", "")
COTURN_CREDENTIAL_TTL_SECONDS = env_int("COTURN_CREDENTIAL_TTL_SECONDS", 1800)  # noqa: F405

# The primary relay is reached as `turn:<host>:<port>` over UDP and
# `turn:<host>:<port>?transport=tcp` over TCP.
COTURN_UDP_TRANSPORT_PARAM = env_bool("COTURN_UDP_TRANSPORT_PARAM", default=False)  # noqa: F405

# No second production relay exists yet, so region 2 stays disabled unless its
# domain is explicitly provided by the environment.
COTURN_REGION2_DOMAIN = os.environ.get("COTURN_REGION2_DOMAIN") or None
COTURN_REGION2_PORT = env_int("COTURN_REGION2_PORT", COTURN_PORT)  # noqa: F405
COTURN_REGION2_TLS_PORT = env_int("COTURN_REGION2_TLS_PORT", COTURN_TLS_PORT)  # noqa: F405
COTURN_REGION2_SHARED_SECRET = os.environ.get("COTURN_REGION2_SHARED_SECRET", "")
