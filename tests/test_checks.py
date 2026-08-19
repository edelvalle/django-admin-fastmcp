"""Startup validation (SPEC.md section 3).

A misconfigured URL must fail at startup, not at the first client call. Before
these checks existed, a wrong MCP_URL surfaced as a bare 401 with nothing in
the logs to explain it.
"""

from django.core.checks import Error
from django.test import override_settings

from django_admin_fastmcp.checks import check_settings


def ids() -> set[str]:
    return {message.id for message in check_settings(None)}


def test_the_demo_project_is_configured_correctly():
    assert [m for m in check_settings(None) if isinstance(m, Error)] == []


@override_settings(ADMIN_FASTMCP={"MCP_URL": "http://127.0.0.1:8765"})
def test_an_mcp_url_without_a_path_is_an_error():
    """A pathless URL would advertise the whole origin as the protected resource."""
    assert "django_admin_fastmcp.E005" in ids()


@override_settings(ADMIN_FASTMCP={"MCP_URL": "127.0.0.1:8765/admin/mcp"})
def test_a_relative_mcp_url_is_an_error():
    assert "django_admin_fastmcp.E005" in ids()


@override_settings(ADMIN_FASTMCP={"SITE_URL": "example.com"})
def test_a_relative_site_url_is_an_error():
    assert "django_admin_fastmcp.E006" in ids()


@override_settings(ADMIN_FASTMCP={"MODELS": ("demo.NoSuchModel",)})
def test_an_unknown_model_is_an_error():
    assert "django_admin_fastmcp.E002" in ids()


@override_settings(ADMIN_FASTMCP={"DISABLED_TOOLS": ("admin_list_models",)})
def test_a_tool_name_with_the_wire_prefix_is_an_error():
    """DISABLED_TOOLS names catalogue keys, not wire names: "list_models"."""
    assert "django_admin_fastmcp.E003" in ids()


@override_settings(ADMIN_FASTMCP={"WRITABLE_MODELS": ("auth.Group",)})
def test_writable_permission_models_warn():
    assert "django_admin_fastmcp.W001" in ids()
