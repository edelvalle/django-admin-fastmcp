"""Settings for the test project (SPEC.md section 2.4).

A real Django project, not settings-only. The `demo` app exists to make the
permission matrix possible (SPEC.md section 11).
"""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

# Make the `demo` app importable both from `manage.py` (cwd inside the
# project) and from pytest (rootdir at the repository top).
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

SECRET_KEY = "test-only-not-a-secret"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_admin_fastmcp",
    "demo",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": PROJECT_DIR / "db.sqlite3",
    },
}

STATIC_URL = "static/"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

ADMIN_FASTMCP = {
    "SERVER_NAME": "demo-admin",
    "EXCLUDE_MODELS": ("auth.Permission", "auth.Group"),
    # ApiCredential stays out on purpose: the matrix needs an exposed,
    # readable model that refuses writes whatever the grant says.
    "WRITABLE_MODELS": ("demo.Book", "demo.Author"),
    "DISABLED_TOOLS": (),
}
