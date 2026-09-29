"""One accessor over the ADMIN_FASTMCP settings dict (SPEC.md section 3).

`get(key)` reads a knob from the consumer's `ADMIN_FASTMCP` dict, with the
defaults below. It raises `KeyError` on an unknown key, which catches typos in
this package at development time. Typos in consumer settings are caught by
`checks.py` at startup.
"""

from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

DEFAULTS: dict[str, Any] = {
    "SERVER_NAME": "django-admin",
    "ADMIN_SITE": "django.contrib.admin.site",
    "MODELS": (),
    "EXCLUDE_MODELS": (),
    "WRITABLE_MODELS": (),
    "DISABLED_TOOLS": (),
    "REDACT_FIELDS": ("password", "token", "secret", "api_key", "private_key"),
    "MAX_PAGE_SIZE": 200,
    "MAX_PKS": 1000,
    "ACCESS_TOKEN_TTL_MINUTES": 60,
    "REFRESH_TOKEN_TTL_DAYS": 90,
    # Minimum gap between last_used_at writes. 0 (or negative) turns them off, so the MCP server can
    # run against a read-only replica.
    "LAST_USED_THROTTLE_MINUTES": 1,
    # Observability (tracing.py). Each backend activates only when its
    # library is importable AND its flag is on; these flags turn one off
    # without uninstalling it.
    "ENABLE_SENTRY_TRACING": True,
    "ENABLE_LOGFIRE_TRACING": True,
    # Base URL of the Django site, the OAuth issuer. Used in discovery
    # metadata and advertised by the MCP server as its authorization server.
    "SITE_URL": "http://127.0.0.1:8000",
    # Public URL of the MCP endpoint, the OAuth resource. Tokens are
    # audience-bound to it, and it is the single source of truth for the path
    # the endpoint is served on. The admin owns "/admin/mcp", next to the
    # OAuth endpoints at "/admin/mcp/authorize" and friends.
    "MCP_URL": "http://127.0.0.1:8765/admin/mcp",
}


def get(key: str) -> Any:
    if key not in DEFAULTS:
        raise KeyError(f"unknown ADMIN_FASTMCP key: {key}")
    configured = getattr(settings, "ADMIN_FASTMCP", {})
    return configured.get(key, DEFAULTS[key])


def mcp_origin() -> str:
    """Scheme and host of MCP_URL, without the path.

    FastMCP builds the resource URL it advertises as `base_url` plus the mount
    path, so `base_url` must not already carry the path. A `base_url` that
    includes it makes discovery advertise the path twice, and then the audience
    the client asks for no longer matches the one the verifier expects.
    """
    parts = urlsplit(get("MCP_URL"))
    return f"{parts.scheme}://{parts.netloc}"


def mcp_path() -> str:
    """The path MCP_URL points at, which is the path the endpoint is served on.

    Always leading-slashed and never trailing-slashed, because FastMCP and
    Starlette both treat "/admin/mcp" and "/admin/mcp/" as different routes.
    `checks.py` refuses an MCP_URL with no path at startup.
    """
    return "/" + urlsplit(get("MCP_URL")).path.strip("/")
