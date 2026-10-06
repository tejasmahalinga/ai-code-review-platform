"""Django settings. All configuration comes from environment variables (12-factor).

See docs/configuration.md for the full reference.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

from apps.core.logging import configure_logging

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


TESTING = env_bool("REVIEWBOT_TESTING")
DEBUG = env_bool("DJANGO_DEBUG")

SECRET_KEY = env("DJANGO_SECRET_KEY") or ""
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = "insecure-dev-only-secret-key-do-not-use-in-production"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set.")

# Public base URL of this instance as seen by browsers and GitHub (no trailing slash).
PUBLIC_URL = (env("REVIEWBOT_PUBLIC_URL", "http://localhost:3000") or "").rstrip("/")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,api")
_public_host = urlparse(PUBLIC_URL).hostname
if _public_host and _public_host not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_public_host)
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", PUBLIC_URL)

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "django_prometheus",
    "apps.core",
    "apps.accounts",
    "apps.credentials",
    "apps.repositories",
    "apps.webhooks",
    "apps.reviews",
    "apps.audit",
]

MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "apps.core.middleware.RequestIdMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]

DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL", "postgres://reviewbot:reviewbot@localhost:5432/reviewbot") or "",
        conn_max_age=env_int("DATABASE_CONN_MAX_AGE", 60),
        conn_health_checks=True,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = env("REDIS_URL", "redis://localhost:6379/0") or ""

if TESTING:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}

AUTH_USER_MODEL = "accounts.User"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
if TESTING:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Sessions & cookies. The dashboard talks to the API through the same origin (Next.js rewrites or a
# reverse proxy), so cookies are first-party.
SECURE_COOKIES = env_bool("REVIEWBOT_SECURE_COOKIES", PUBLIC_URL.startswith("https://"))
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = SECURE_COOKIES
SESSION_COOKIE_AGE = env_int("REVIEWBOT_SESSION_AGE_SECONDS", 60 * 60 * 24 * 14)
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = SECURE_COOKIES
BEHIND_PROXY = env_bool("REVIEWBOT_BEHIND_PROXY")
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if BEHIND_PROXY else None
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.core.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.DefaultCursorPagination",
    "EXCEPTION_HANDLER": "apps.core.exceptions.exception_handler",
    "DEFAULT_THROTTLE_RATES": {
        "login": env("REVIEWBOT_LOGIN_RATE", "5/min"),
        "setup": "10/min",
        "invite": "30/min",
    },
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Reviewbot API",
    "DESCRIPTION": "REST API for the Reviewbot dashboard.",
    "VERSION": "0.2.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

# --- Celery -------------------------------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = None
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = env_int("REVIEWBOT_REVIEW_TIME_LIMIT_SECONDS", 15 * 60)
CELERY_TASK_SOFT_TIME_LIMIT = CELERY_TASK_TIME_LIMIT - 60
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ROUTES = {
    "apps.reviews.tasks.run_review": {"queue": "reviews"},
    "apps.repositories.tasks.*": {"queue": "default"},
}
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", TESTING)
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_BEAT_SCHEDULE = {
    "reap-stuck-reviews": {"task": "apps.reviews.tasks.reap_stuck_runs", "schedule": 300.0},
    "prune-webhook-deliveries": {"task": "apps.webhooks.tasks.prune_deliveries", "schedule": 3600.0},
    "prune-audit-events": {"task": "apps.audit.tasks.prune_audit_events", "schedule": 86400.0},
}

# --- Reviewbot ----------------------------------------------------------------------------------
# Comma-separated Fernet keys. The first key encrypts; all keys decrypt (enables master-key rotation).
REVIEWBOT_ENCRYPTION_KEYS = env_list("REVIEWBOT_ENCRYPTION_KEYS")
if not REVIEWBOT_ENCRYPTION_KEYS and TESTING:
    REVIEWBOT_ENCRYPTION_KEYS = ["dGVzdC1rZXktdGVzdC1rZXktdGVzdC1rZXktdGVzdC0="]

GITHUB_URL = (env("GITHUB_URL", "https://github.com") or "").rstrip("/")
GITHUB_API_URL = (env("GITHUB_API_URL", "https://api.github.com") or "").rstrip("/")

# Enables the "fake" LLM provider (deterministic canned findings, no network). For demos and E2E tests.
REVIEWBOT_ENABLE_FAKE_PROVIDER = env_bool("REVIEWBOT_ENABLE_FAKE_PROVIDER", TESTING)

METRICS_TOKEN = env("REVIEWBOT_METRICS_TOKEN", "")
STUCK_RUN_MINUTES = env_int("REVIEWBOT_STUCK_RUN_MINUTES", 30)
# Wait this long after a push before reviewing it; newer pushes in the window supersede it.
REVIEWBOT_PUSH_DEBOUNCE_SECONDS = env_int("REVIEWBOT_PUSH_DEBOUNCE_SECONDS", 60)
WEBHOOK_RETENTION_DAYS = env_int("REVIEWBOT_WEBHOOK_RETENTION_DAYS", 30)
AUDIT_RETENTION_DAYS = env_int("REVIEWBOT_AUDIT_RETENTION_DAYS", 365)
INVITE_TTL_HOURS = env_int("REVIEWBOT_INVITE_TTL_HOURS", 72)

# --- Sign-in with GitHub (ADM-03) ---------------------------------------------------------------
# Uses the GitHub App's OAuth client by default; set these to use a separate OAuth App instead.
GITHUB_OAUTH_CLIENT_ID = env("REVIEWBOT_GITHUB_OAUTH_CLIENT_ID", "") or ""
GITHUB_OAUTH_CLIENT_SECRET = env("REVIEWBOT_GITHUB_OAUTH_CLIENT_SECRET", "") or ""
GITHUB_LOGIN_ENABLED = env_bool("REVIEWBOT_GITHUB_LOGIN", True)
# Without an invite, GitHub users can only sign in to an existing account (matched by verified email),
# unless self-signup is allowed. Signup can be limited to verified email domains (comma-separated).
ALLOW_SIGNUP = env_bool("REVIEWBOT_ALLOW_SIGNUP", False)
SIGNUP_EMAIL_DOMAINS = [d.lower().lstrip("@") for d in env_list("REVIEWBOT_SIGNUP_EMAIL_DOMAINS", "")]
SIGNUP_ROLE = env("REVIEWBOT_SIGNUP_ROLE", "viewer") or "viewer"

# --- Email (optional; used to send invites) -----------------------------------------------------
EMAIL_HOST = env("REVIEWBOT_EMAIL_HOST", "") or ""
EMAIL_PORT = env_int("REVIEWBOT_EMAIL_PORT", 587)
EMAIL_HOST_USER = env("REVIEWBOT_EMAIL_USER", "") or ""
EMAIL_HOST_PASSWORD = env("REVIEWBOT_EMAIL_PASSWORD", "") or ""
EMAIL_USE_TLS = env_bool("REVIEWBOT_EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("REVIEWBOT_EMAIL_FROM", "reviewbot@localhost") or "reviewbot@localhost"
if TESTING:
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_ENABLED = bool(EMAIL_HOST) or TESTING
LLM_TIMEOUT_SECONDS = env_int("REVIEWBOT_LLM_TIMEOUT_SECONDS", 180)
LLM_MAX_RETRIES = env_int("REVIEWBOT_LLM_MAX_RETRIES", 4)
GIT_TIMEOUT_SECONDS = env_int("REVIEWBOT_GIT_TIMEOUT_SECONDS", 30)

# --- Logging ------------------------------------------------------------------------------------
LOG_LEVEL = (env("LOG_LEVEL", "INFO") or "INFO").upper()
LOG_FORMAT = env("LOG_FORMAT", "console" if DEBUG else "json")

LOGGING_CONFIG = None
configure_logging(LOG_LEVEL, LOG_FORMAT or "json")

SENTRY_DSN = env("SENTRY_DSN", "")
