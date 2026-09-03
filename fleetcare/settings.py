"""
Django settings for fleetcare project.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
# Local dev reads a .env file; Render (and any real host) supplies real
# env vars directly, so a missing .env there is expected, not an error.
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Render terminates TLS at its proxy and forwards plain HTTP with this
# header; without it Django thinks every request is insecure and CSRF /
# is_secure() checks behave as if there's no HTTPS at all.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'accounts',
    'fleet',
]

# Must be set before the first migrate; Django has no supported way to swap
# the user model afterwards short of a full database reset.
AUTH_USER_MODEL = 'accounts.User'

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # must sit right after SecurityMiddleware
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'fleetcare.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'fleet.context_processors.alerts',
            ],
        },
    },
]

WSGI_APPLICATION = 'fleetcare.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    # Falls back to local sqlite when DATABASE_URL isn't set, so `manage.py`
    # commands work without a Postgres instance running on a dev machine.
    'default': env.db('DATABASE_URL', default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    "staticfiles": {
        # Not CompressedManifestStaticFilesStorage: its {% static %} URL
        # resolution reads collectstatic's manifest AND opens the file
        # under STATIC_ROOT to hash it, and `manage.py test` never runs
        # collectstatic -- every test that renders base.html (now that it
        # loads a stylesheet) would fail with "file could not be found"
        # even with manifest_strict=False, since STATIC_ROOT is empty
        # before collectstatic ever runs once. CompressedStaticFilesStorage
        # still gzip/brotli-compresses files during collectstatic for
        # production, it just resolves {% static %} URLs by string
        # concatenation (STATIC_URL + path) instead of a disk/manifest
        # lookup, so it works identically whether or not collectstatic has
        # run. Trade-off: no cache-busting hashed filenames -- acceptable
        # for a single hand-written stylesheet with no build step.
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Auth

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'


# Fleet service lifecycle

# A DUE record counts as overdue once it's been due longer than this many
# days -- never stored (no is_overdue column), always derived at read time
# from due_since. Env-configurable since "how much grace" is an operational
# call, not a code constant.
SERVICE_GRACE_PERIOD_DAYS = env.int('SERVICE_GRACE_PERIOD_DAYS', default=7)
