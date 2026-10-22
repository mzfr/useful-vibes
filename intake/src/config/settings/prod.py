from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# No fallback in production — boot must fail loudly if the key is missing,
# otherwise sessions are forgeable with the publicly-known dev key.
SECRET_KEY = env("INTAKE_SECRET_KEY", required=True)

# "*" is not acceptable in prod (DNS-rebinding / host-header attacks on a
# plain-HTTP LAN deployment). Operator must list the hosts they serve on.
ALLOWED_HOSTS = [
    h.strip()
    for h in env("INTAKE_ALLOWED_HOSTS", required=True).split(",")
    if h.strip()
]
if "*" in ALLOWED_HOSTS:
    raise RuntimeError(
        "INTAKE_ALLOWED_HOSTS must not contain '*' in production — "
        "list the hostnames/IPs you actually serve on."
    )

# TLS-dependent settings are gated: the default self-host deployment is plain
# HTTP on :8000 (LAN). Secure cookies over plain HTTP are silently dropped by
# browsers for anything but localhost → login loops with no visible error.
# Set INTAKE_BEHIND_TLS=1 when running behind an HTTPS reverse proxy.
_BEHIND_TLS = env("INTAKE_BEHIND_TLS", "0") == "1"

if _BEHIND_TLS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
