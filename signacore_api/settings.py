from __future__ import annotations

import os
import sys
from pathlib import Path

from corsheaders.defaults import default_headers
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured

from utils.observability import configure_error_reporting

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_dotenv_file(BASE_DIR / ".env")


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    value = env(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int) -> int:
    value = env(key)
    if value is None:
        return default
    return int(value)


def env_csv(key: str, default: str = "") -> list[str]:
    raw = env(key, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


DEBUG = env_bool("DEBUG", True)
SECRET_KEY = env("SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("SECRET_KEY must be configured when DEBUG=False.")
    SECRET_KEY = "signacore-local-development-key-do-not-use-in-production"
if not DEBUG and len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("SECRET_KEY must contain at least 50 characters when DEBUG=False.")
ALLOWED_HOSTS = env_csv("ALLOWED_HOSTS", "127.0.0.1,localhost")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "corsheaders",
    "apps.accounts",
    "apps.billing",
    "apps.documents",
    "apps.signing",
    "apps.notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "signacore_api.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "signacore_api.wsgi.application"
ASGI_APPLICATION = "signacore_api.asgi.application"

DATABASE_NAME = env("DB_NAME")
if DATABASE_NAME:
    database_options = {}
    database_sslmode = env("DB_SSLMODE")
    if database_sslmode:
        database_options["sslmode"] = database_sslmode
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": DATABASE_NAME,
            "USER": env("DB_USER", ""),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "5432"),
            "OPTIONS": database_options,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_RESET_TIMEOUT = 24 * 60 * 60

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = Path(env("STATIC_ROOT", str(BASE_DIR / "staticfiles")))

MEDIA_URL = env("MEDIA_URL", "/media/")
SIGNACORE_STORAGE_ROOT = Path(
    env(
        "SIGNACORE_STORAGE_ROOT",
        env("MEDIA_ROOT", str(BASE_DIR / "storage")),
    )
)
MEDIA_ROOT = SIGNACORE_STORAGE_ROOT
SIGNACORE_MAX_DOCUMENT_UPLOAD_BYTES = env_int("SIGNACORE_MAX_DOCUMENT_UPLOAD_BYTES", 25 * 1024 * 1024)
if SIGNACORE_MAX_DOCUMENT_UPLOAD_BYTES <= 0:
    raise ImproperlyConfigured("SIGNACORE_MAX_DOCUMENT_UPLOAD_BYTES must be greater than zero.")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", not DEBUG)
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 31536000 if not DEBUG else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", not DEBUG)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", not DEBUG)
CSRF_TRUSTED_ORIGINS = env_csv("CSRF_TRUSTED_ORIGINS")

if "test" in sys.argv:
    STATIC_ROOT = BASE_DIR / "staticfiles-test"
    SIGNACORE_STORAGE_ROOT = BASE_DIR / "storage-test"
    MEDIA_ROOT = SIGNACORE_STORAGE_ROOT
    # Django's test client uses HTTP internally; production HTTPS checks must
    # not redirect every test request before it reaches the API view.
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


def throttle_rate(scope: str, default: str) -> str:
    """A rate that can be changed without a release.

    These were literals, so the limits could not be relaxed on staging - for a load test, say -
    or tightened in production under abuse, without editing code and deploying it.
    """
    return (env(f"SIGNACORE_THROTTLE_{scope.upper()}", default) or default).strip()


REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_RATES": {
        "admin_auth": throttle_rate("admin_auth", "5/minute"),
        "email_auth": throttle_rate("email_auth", "10/minute"),
        "oauth_exchange": throttle_rate("oauth_exchange", "10/minute"),
        "account_data": throttle_rate("account_data", "60/minute"),
        "signer_context": throttle_rate("signer_context", "60/minute"),
        # Reading a document costs one request per page, plus a thumbnail of each. A 31-page
        # packet is 62 requests to read through once, so the old 60 a minute was less than one
        # pass: a signer could run out of allowance on their own document, and see the rest of it
        # as broken images.
        "signer_preview": throttle_rate("signer_preview", "120/minute"),
        "signer_otp_send": throttle_rate("signer_otp_send", "5/minute"),
        "signer_otp_verify": throttle_rate("signer_otp_verify", "10/minute"),
        "signer_submit": throttle_rate("signer_submit", "10/minute"),
        "document_import": throttle_rate("document_import", "10/minute"),
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "SignaCore API",
    "DESCRIPTION": (
        "Self-hosted document signing API for SignaCore. "
        "The admin surface uses Django-backed admin users and the public signer flow uses unique signing tokens."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
    "TAGS": [
        {"name": "health", "description": "Service health checks."},
        {"name": "admin", "description": "Admin console operations proxied by the SignaCore web app."},
        {"name": "signer", "description": "Public signer workflow endpoints."},
    ],
    "ENUM_NAME_OVERRIDES": {
        "SignacorePlan": (
            ("FREE", "Free"),
            ("PROFESSIONAL", "Professional"),
            ("BUSINESS", "Business"),
            ("ENTERPRISE", "Enterprise"),
        ),
        "SignacoreCheckoutPlan": (
            ("PROFESSIONAL", "Professional"),
            ("BUSINESS", "Business"),
        ),
    },
}

CORS_ALLOWED_ORIGINS = env_csv("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_HEADERS = tuple(default_headers) + ("x-signacore-secret",)

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", "smtp.resend.com")
EMAIL_PORT = env_int("EMAIL_PORT", 465)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", True)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "resend")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "SignaCore - by Se7en <signacore@mysignacore.com>")

REDIS_URL = env("REDIS_URL", "redis://127.0.0.1:6379/3")
CELERY_BROKER_URL = env("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", REDIS_URL)
# Every module holding a task, named rather than discovered.
#
# ``autodiscover_tasks()`` looks for a ``tasks`` module inside each entry of INSTALLED_APPS. These
# tasks live in a top-level ``tasks`` package instead, which is in none of them, so it found
# nothing. What registered anyway did so by accident: the views import ``tasks.notifications`` at
# module level, so loading Django pulled it in. ``tasks.signing`` and ``tasks.documents`` are
# imported nowhere at module level, so a worker never saw them, and beat published both scheduled
# tasks to a worker that discarded them as unknown.
CELERY_IMPORTS = (
    "tasks.documents",
    "tasks.maintenance",
    "tasks.marketing",
    "tasks.notifications",
    "tasks.signing",
)
CELERY_BEAT_SCHEDULE = {
    # Hourly rather than daily so a signature at any hour is followed up roughly a day later
    # rather than up to two. The task itself decides whether anything is due, and does nothing
    # at all unless the follow-up is switched on.
    "signer-follow-ups-hourly": {
        "task": "tasks.marketing.send_signer_follow_ups",
        "schedule": 3600,
    },
    "expire-signing-links-hourly": {
        "task": "tasks.signing.expire_signing_links",
        "schedule": 3600,
    },
    # A document everyone has signed but which never received its completed copy is invisible
    # until someone goes looking, so this looks regularly rather than waiting to be asked.
    "issue-outstanding-completed-documents": {
        "task": "tasks.signing.issue_outstanding_completed_documents",
        "schedule": 900,
    },
    # Nothing ever removed anything, so encrypted files accumulated for as long as the service
    # had been running. Nightly is often enough for a leak and rare enough that a mistake in it
    # has a day to be noticed.
    "clean-up-nightly": {
        "task": "tasks.maintenance.clean_up",
        "schedule": 24 * 3600,
    },
}

FERNET_KEY = env("FERNET_KEY")
if not FERNET_KEY and "test" in sys.argv:
    FERNET_KEY = "WtF08bpUcDm4uvofwRRmm-JO-17mi6T6qwrmeJEC9Gc="
if not FERNET_KEY:
    raise ImproperlyConfigured("FERNET_KEY must be configured; SignaCore cannot start without encryption.")
try:
    Fernet(FERNET_KEY.encode())
except (TypeError, ValueError) as exc:
    raise ImproperlyConfigured(
        "FERNET_KEY must be a valid 32-byte url-safe base64-encoded key. "
        "Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
    ) from exc
SIGNACORE_TEMP_ROOT = Path(env("SIGNACORE_TEMP_ROOT", "/run/signacore"))
FILE_UPLOAD_TEMP_DIR = str(SIGNACORE_TEMP_ROOT)
SIGNACORE_SHARED_SECRET = env("SIGNACORE_SHARED_SECRET", "")
SIGNACORE_SERVICE_USERNAME = env("SIGNACORE_SERVICE_USERNAME", "signacore-service")
SIGNACORE_APP_URL = env("SIGNACORE_APP_URL", "https://mysignacore.com")
SIGNACORE_SIGNER_PORTAL_URL = env("SIGNACORE_SIGNER_PORTAL_URL", "https://sign.mysignacore.com")
# How long a document is kept after its last activity. Zero, the default, keeps everything: the
# cleanup deletes no document until somebody chooses a number, because the right number is a
# policy decision and the wrong one is irreversible.
SIGNACORE_DOCUMENT_RETENTION_DAYS = env_int("SIGNACORE_DOCUMENT_RETENTION_DAYS", 0)
# The follow-up to signers is the only mail SignaCore sends that the recipient is not already
# expecting, so it is off until somebody decides the sending domain is ready to carry it.
SIGNACORE_SIGNER_FOLLOW_UP_ENABLED = env_bool("SIGNACORE_SIGNER_FOLLOW_UP_ENABLED", False)
SIGNING_LINK_EXPIRY_DAYS = env_int("SIGNING_LINK_EXPIRY_DAYS", 7)
OTP_EXPIRY_MINUTES = env_int("OTP_EXPIRY_MINUTES", 10)
OTP_RESEND_COOLDOWN_SECONDS = env_int("OTP_RESEND_COOLDOWN_SECONDS", 60)

STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", "") or ""
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", "") or ""
STRIPE_API_VERSION = env("STRIPE_API_VERSION", "2026-02-25.clover") or "2026-02-25.clover"

GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", "") or ""
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", "") or ""
APPLE_OAUTH_CLIENT_ID = env("APPLE_OAUTH_CLIENT_ID", "") or ""
APPLE_OAUTH_TEAM_ID = env("APPLE_OAUTH_TEAM_ID", "") or ""
APPLE_OAUTH_KEY_ID = env("APPLE_OAUTH_KEY_ID", "") or ""
APPLE_OAUTH_PRIVATE_KEY = (env("APPLE_OAUTH_PRIVATE_KEY", "") or "").replace("\\n", "\n")

# --- Error reporting -------------------------------------------------------
# Off unless a DSN is configured, so development, tests and any deployment without one are
# unaffected. What is sent is decided in utils.observability, not left to the SDK's defaults.
SENTRY_DSN = env("SENTRY_DSN", "") or ""
SENTRY_ENVIRONMENT = env("SENTRY_ENVIRONMENT", "staging") or "staging"
SENTRY_RELEASE = env("SENTRY_RELEASE", "") or ""


def _sentry_traces_sample_rate() -> float:
    try:
        rate = float(env("SENTRY_TRACES_SAMPLE_RATE", "0.1") or 0.1)
    except (TypeError, ValueError):
        return 0.1
    return min(max(rate, 0.0), 1.0)


SENTRY_ENABLED = configure_error_reporting(
    # A test run is not an incident. The suite deliberately provokes failures - an unsanitisable
    # document, a submission that cannot complete - and with a DSN in the developer's .env every
    # one of them was reported against the production project, which is precisely the signal an
    # alert on new issues is meant to carry.
    dsn="" if "test" in sys.argv else SENTRY_DSN,
    environment=SENTRY_ENVIRONMENT,
    release=SENTRY_RELEASE,
    traces_sample_rate=_sentry_traces_sample_rate(),
    debug=False,
)
