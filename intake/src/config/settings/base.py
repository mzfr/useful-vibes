"""
Base settings shared by dev and prod.

Environment variables are read here once and re-exported as module attributes
so the rest of the codebase imports from `django.conf.settings`.
"""
from __future__ import annotations

import os
from pathlib import Path

# src/ is the import root; manage.py lives there.
BASE_DIR = Path(__file__).resolve().parent.parent.parent
# Repo root holds pyproject.toml, .env, /data/, etc.
REPO_ROOT = BASE_DIR.parent


def env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Environment variable {name} is required.")
    return value


SECRET_KEY = env("INTAKE_SECRET_KEY", "dev-insecure-key-do-not-use-in-prod")
DEBUG = env("INTAKE_DEBUG", "0") == "1"
ALLOWED_HOSTS = [h for h in env("INTAKE_ALLOWED_HOSTS", "*").split(",") if h]

# --- Apps ------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "simple_history",
]

LOCAL_APPS = [
    "core",
    "tracking",
    "releases",
    "sharing",
    "stats",
    "imports",
    "sources",
    "api",
    "ui",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "ui" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.app_meta",
                "core.context_processors.user_messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- Database --------------------------------------------------------------

DB_PATH = Path(env("INTAKE_DB_PATH", str(REPO_ROOT / "data" / "db.sqlite3")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(DB_PATH),
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=NORMAL;"
                "PRAGMA foreign_keys=ON;"
                "PRAGMA busy_timeout=5000;"
            ),
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Cache -----------------------------------------------------------------
# Single-process; the LocMem backend is in-memory per-worker, which is fine
# for our 2-worker gunicorn deployment. No Redis dependency.

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "intake-default",
    },
    "providers": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "intake-providers",
        "TIMEOUT": 60 * 60 * 6,  # 6h default; per-call TTLs override below
        "OPTIONS": {"MAX_ENTRIES": 5000},
    },
}

# Per-call TTLs (seconds), tunable without code changes if needed.
PROVIDER_CACHE_TTLS = {
    "details": 60 * 60 * 6,   # 6h — title/synopsis/runtime barely move
    "season":  60 * 30,       # 30min — episode air dates can shift late
    "episode": 60 * 30,
    "search":  60 * 10,       # 10min — keep search results snappy
}

# --- Auth ------------------------------------------------------------------

LOGIN_URL = "/login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
]

# --- I18N / TZ -------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("INTAKE_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

# --- Static / Media --------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = REPO_ROOT / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "ui" / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# --- Provider credentials --------------------------------------------------

TMDB_API_KEY = env("TMDB_API_KEY", "")
# Public static-history origin. It is used only to display generated links in
# Settings; Intake never serves or fetches this URL itself.
INTAKE_HISTORY_PUBLIC_URL = env("INTAKE_HISTORY_PUBLIC_URL", "")
# No Trakt API credentials: their API is VIP-only now, so imports read the
# official data export ZIP instead (see imports/trakt_export.py).

# --- Bootstrap (first-run admin user) --------------------------------------

INTAKE_INITIAL_PASSWORD = env("INTAKE_INITIAL_PASSWORD", "")
INTAKE_INITIAL_USERNAME = env("INTAKE_INITIAL_USERNAME", "admin")

# --- App metadata ----------------------------------------------------------

APP_NAME = "Intake"
APP_TAGLINE = "What did you watch?"
