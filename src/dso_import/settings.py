import os

import environ
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration

env = environ.Env()

# -- Environment

BASE_DIR = str(environ.Path(__file__) - 2)
DEBUG = env.bool("DJANGO_DEBUG", False)

# Paths
STATIC_URL = "/v1/static/"
STATIC_ROOT = "/static/"

DATAPUNT_API_URL = env.str("DATAPUNT_API_URL", "https://api.data.amsterdam.nl/")
SCHEMA_URL = env.str("SCHEMA_URL", "https://schemas.data.amsterdam.nl/datasets/")
SCHEMA_DEFS_URL = env.str("SCHEMA_DEFS_URL", "https://schemas.data.amsterdam.nl/schema")


# -- Security

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = env.str("SECRET_KEY", "insecure")

SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", not DEBUG)

# On unapplied migrations, the Django 'check' fails when trying to
# Fetch datasets from the database. Viewsets are not needed when migrating.
INITIALIZE_DYNAMIC_VIEWSETS = env.bool("INITIALIZE_DYNAMIC_VIEWSETS", True)

INTERNAL_IPS = ("127.0.0.1", "0.0.0.0")


# -- Application definition

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "django.contrib.postgres",
    # "django_filters",
    "drf_spectacular",
    "rest_framework",
    "rest_framework_gis",
    "gisserver",
    # Own apps
    "schematools.contrib.django",
    "dso_import",
]

MIDDLEWARE = [
]

# -- Services

DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default="postgres://dataservices:insecure@localhost:5415/dataservices",
        engine="django.contrib.gis.db.backends.postgis",
    ),
}

locals().update(env.email_url(default="smtp://"))

SENTRY_DSN = env.str("SENTRY_DSN", default="")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN, environment="dso-import", integrations=[DjangoIntegration()]
    )

TIME_ZONE = 'Europe/Amsterdam'

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {"format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"},
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "console",
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        # 'django.db.backends': {
        #     'level': 'DEBUG',
        #     'handlers': ['console'],
        # },
        "dso_import": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
    },
}


PROJECT_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))
DATA_DIR = os.getenv("DATA_DIR", os.path.abspath(os.path.join(PROJECT_DIR, "data")))

AMSTERDAM_SCHEMA = {"geosearch_disabled_datasets": ["bag"]}
