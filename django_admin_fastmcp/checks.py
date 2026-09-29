"""Startup validation of the ADMIN_FASTMCP settings (SPEC.md section 3).

A bad value is an error, not a warning. Exceptions: auth.Permission and
auth.Group exposed and writable is a warning (SPEC.md section 7), because it
is dangerous rather than broken.
"""

from urllib.parse import urlsplit

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, Warning, register
from django.utils.module_loading import import_string

from django_admin_fastmcp import conf

PERMISSION_MODELS = ("auth.permission", "auth.group")


def _resolves(label: str) -> bool:
    if label.endswith(".*"):
        return apps.is_installed(label[:-2]) or bool(
            [m for m in apps.get_models() if m._meta.app_label == label[:-2]]
        )
    try:
        apps.get_model(label)
    except (LookupError, ValueError):
        return False
    return True


@register()
def check_settings(app_configs, **kwargs):
    from django_admin_fastmcp.server import all_tools

    errors = []
    configured = getattr(settings, "ADMIN_FASTMCP", {})

    for key in configured:
        if key not in conf.DEFAULTS:
            errors.append(
                Error(
                    f"unknown ADMIN_FASTMCP key: {key!r}",
                    hint=f"known keys: {sorted(conf.DEFAULTS)}",
                    id="django_admin_fastmcp.E001",
                )
            )

    for setting in ("MODELS", "EXCLUDE_MODELS", "WRITABLE_MODELS"):
        for label in conf.get(setting):
            if not _resolves(label):
                errors.append(
                    Error(
                        f"ADMIN_FASTMCP[{setting!r}] names {label!r}, "
                        "which is not an installed model",
                        id="django_admin_fastmcp.E002",
                    )
                )

    known_tools = set(all_tools())
    for name in conf.get("DISABLED_TOOLS"):
        if name not in known_tools:
            errors.append(
                Error(
                    f"ADMIN_FASTMCP['DISABLED_TOOLS'] names {name!r}, "
                    f"which is not a tool. Tools: {sorted(known_tools)}",
                    id="django_admin_fastmcp.E003",
                )
            )

    try:
        import_string(conf.get("ADMIN_SITE"))
    except ImportError:
        errors.append(
            Error(
                f"ADMIN_FASTMCP['ADMIN_SITE'] does not import: {conf.get('ADMIN_SITE')!r}",
                id="django_admin_fastmcp.E004",
            )
        )

    # MCP_URL is the path the endpoint is served on and the audience every
    # token is bound to. A URL with no path would advertise the whole origin
    # as the protected resource, and the served path would collapse to "/".
    mcp_parts = urlsplit(conf.get("MCP_URL"))
    if (
        mcp_parts.scheme not in ("http", "https")
        or not mcp_parts.netloc
        or not mcp_parts.path.strip("/")
    ):
        errors.append(
            Error(
                f"ADMIN_FASTMCP['MCP_URL'] must be an absolute http(s) URL with a path, "
                f"got {conf.get('MCP_URL')!r}",
                hint="for example https://admin.example.com/admin/mcp",
                id="django_admin_fastmcp.E005",
            )
        )

    site_parts = urlsplit(conf.get("SITE_URL"))
    if site_parts.scheme not in ("http", "https") or not site_parts.netloc:
        errors.append(
            Error(
                f"ADMIN_FASTMCP['SITE_URL'] must be an absolute http(s) URL, "
                f"got {conf.get('SITE_URL')!r}",
                hint="it is the OAuth issuer, the Django site that serves the admin login",
                id="django_admin_fastmcp.E006",
            )
        )

    throttle_minutes = conf.get("LAST_USED_THROTTLE_MINUTES")
    if type(throttle_minutes) is not int:
        errors.append(
            Error(
                "ADMIN_FASTMCP['LAST_USED_THROTTLE_MINUTES'] must be an int, "
                f"got {throttle_minutes!r}",
                hint="use 0 or a negative value to turn last_used_at updates off",
                id="django_admin_fastmcp.E007",
            )
        )

    writable = [w.lower() for w in conf.get("WRITABLE_MODELS")]
    for label in PERMISSION_MODELS:
        if label in writable:
            errors.append(
                Warning(
                    f"{label} is writable over MCP. An agent that can grant "
                    "permissions can escape the permission model.",
                    hint="remove it from ADMIN_FASTMCP['WRITABLE_MODELS']",
                    id="django_admin_fastmcp.W001",
                )
            )
    return errors
