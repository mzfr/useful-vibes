from .base import *  # noqa: F401,F403
from .base import INSTALLED_APPS, MIDDLEWARE  # noqa: F401

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Loosen storage in dev so missing assets don't break manifest hashing.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

INTERNAL_IPS = ["127.0.0.1"]
