from __future__ import annotations

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

APP_NAME = os.environ.get("APP_NAME", "ReceiptHub")

SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-local-dev-only-change-me")
DEBUG = os.environ.get("DEBUG", "True").lower() in {"1", "true", "yes", "on"}

def csv_env(name: str, default: str = "") -> list[str]:
    return [value.strip() for value in os.environ.get(name, default).split(",") if value.strip()]


def bool_env(name: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return os.environ.get(name, fallback).lower() in {"1", "true", "yes", "on"}


def int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default

ALLOWED_HOSTS = csv_env("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
if RAILWAY_PUBLIC_DOMAIN and RAILWAY_PUBLIC_DOMAIN not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RAILWAY_PUBLIC_DOMAIN)
if "healthcheck.railway.app" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("healthcheck.railway.app")

CSRF_TRUSTED_ORIGINS = csv_env("CSRF_TRUSTED_ORIGINS")
if RAILWAY_PUBLIC_DOMAIN:
    railway_origin = f"https://{RAILWAY_PUBLIC_DOMAIN}"
    if railway_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(railway_origin)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "receipts",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "receipts.middleware.ForcePasswordChangeMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "auto_receipt.urls"

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
                "receipts.context_processors.app_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "auto_receipt.wsgi.application"

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
elif os.environ.get("PGHOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "railway"),
            "USER": os.environ.get("PGUSER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD", ""),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
            "CONN_MAX_AGE": 600,
            "CONN_HEALTH_CHECKS": True,
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

LANGUAGE_CODE = "ja"
TIME_ZONE = os.environ.get("TIME_ZONE", "Asia/Tokyo")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.environ.get("RECEIPT_MEDIA_ROOT", BASE_DIR / "media"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", 10 * 1024 * 1024))
FILE_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE + 1024 * 1024

RECEIPT_RETENTION_MONTHS = min(max(int(os.environ.get("RECEIPT_RETENTION_MONTHS", "3")), 1), 3)
ALLOW_SIGNUP = bool_env("ALLOW_SIGNUP", True)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")
RECEIPT_AI_FILENAME_ENABLED = bool_env("RECEIPT_AI_FILENAME_ENABLED", True)
RECEIPT_CARD_LAST4 = os.environ.get("RECEIPT_CARD_LAST4", "7210")
RECEIPT_AI_MANUAL_BATCH_SIZE = min(max(int_env("RECEIPT_AI_MANUAL_BATCH_SIZE", 100), 1), 500)
try:
    RECEIPT_AI_TIMEOUT = float(os.environ.get("RECEIPT_AI_TIMEOUT", "30"))
except ValueError:
    RECEIPT_AI_TIMEOUT = 30.0

# SMTP / email settings. Resend SMTP uses username "resend" and an API key as the password.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int_env("SMTP_PORT", 587)
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "noreply@example.com")
SMTP_STARTTLS = bool_env("SMTP_STARTTLS", True)
SMTP_SSL = bool_env("SMTP_SSL", False)
SMTP_TIMEOUT_SECONDS = int_env("SMTP_TIMEOUT_SECONDS", 12)
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
if not APP_BASE_URL and RAILWAY_PUBLIC_DOMAIN:
    APP_BASE_URL = f"https://{RAILWAY_PUBLIC_DOMAIN}"

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = SMTP_HOST
EMAIL_PORT = SMTP_PORT
EMAIL_HOST_USER = SMTP_USERNAME
EMAIL_HOST_PASSWORD = SMTP_PASSWORD
EMAIL_USE_TLS = SMTP_STARTTLS
EMAIL_USE_SSL = SMTP_SSL
EMAIL_TIMEOUT = SMTP_TIMEOUT_SECONDS
DEFAULT_FROM_EMAIL = SMTP_FROM
SERVER_EMAIL = SMTP_FROM
RECEIPT_REMINDER_TARGET_MONTH_OFFSET = int_env("RECEIPT_REMINDER_TARGET_MONTH_OFFSET", 0)

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
if not DEBUG:
    SECURE_SSL_REDIRECT = bool_env("SECURE_SSL_REDIRECT", False)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    CSRF_COOKIE_HTTPONLY = False
    X_FRAME_OPTIONS = "DENY"
