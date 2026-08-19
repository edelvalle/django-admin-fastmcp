"""Scaffolding smoke test: the package imports and Django is configured."""

import django_admin_fastmcp


def test_version():
    assert django_admin_fastmcp.__version__


def test_app_is_installed(settings):
    assert "django_admin_fastmcp" in settings.INSTALLED_APPS
