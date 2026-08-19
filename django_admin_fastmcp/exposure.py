"""Which models are visible, which fields are redacted (SPEC.md section 7).

Default: every model in `admin_site._registry`. Narrowed by EXCLUDE_MODELS
(with "app_label.*" support), the MODELS allowlist, and `mcp_expose = False`
on a ModelAdmin. A built-in denylist cannot be overridden: every
`django_admin_fastmcp` model, `sessions.Session`, and `authtoken.Token`.
"""

from functools import cache

from django.apps import apps
from django.contrib.admin import AdminSite, ModelAdmin
from django.db.models import Model
from django.utils.module_loading import import_string

from django_admin_fastmcp import conf

# Written through MCP, these would let an agent escalate outside the audit
# trail. Never exposed, whatever the settings say.
BUILTIN_DENY_APPS = frozenset({"django_admin_fastmcp"})
BUILTIN_DENY_MODELS = frozenset({"sessions.session", "authtoken.token"})


@cache
def admin_site() -> AdminSite:
    return import_string(conf.get("ADMIN_SITE"))


def label_of(model: type[Model]) -> str:
    return f"{model._meta.app_label}.{model.__name__}"


def _normalize(label: str) -> str:
    return label.lower()


def _matches(label: str, patterns) -> bool:
    """True when `label` matches an exact "app.Model" or an "app.*" pattern."""
    norm = _normalize(label)
    app_label = norm.split(".", 1)[0]
    for pattern in patterns:
        pattern = _normalize(pattern)
        if pattern == norm:
            return True
        if pattern.endswith(".*") and pattern[:-2] == app_label:
            return True
    return False


def is_exposed(label: str) -> bool:
    """Fail closed: an unresolvable label is not exposed."""
    try:
        model = apps.get_model(label)
    except (LookupError, ValueError):
        return False
    norm = _normalize(label_of(model))
    if norm.split(".", 1)[0] in BUILTIN_DENY_APPS or norm in BUILTIN_DENY_MODELS:
        return False
    if _matches(norm, conf.get("EXCLUDE_MODELS")):
        return False
    allowlist = conf.get("MODELS")
    if allowlist and not _matches(norm, allowlist):
        return False
    model_admin = admin_site()._registry.get(model)
    if model_admin is None:
        return False
    return getattr(model_admin, "mcp_expose", True) is not False


def exposed_models() -> list[tuple[type[Model], ModelAdmin]]:
    return [
        (model, model_admin)
        for model, model_admin in admin_site()._registry.items()
        if is_exposed(label_of(model))
    ]


def is_writable(label: str) -> bool:
    """Both gates: the model is exposed and named in WRITABLE_MODELS."""
    return is_exposed(label) and _matches(label, conf.get("WRITABLE_MODELS"))


def visible_field_names(model_admin: ModelAdmin, all_names: list[str]) -> list[str]:
    """Apply the per-ModelAdmin `mcp_fields` allowlist and
    `mcp_exclude_fields` denylist to a list of field names."""
    allow = getattr(model_admin, "mcp_fields", None)
    deny = getattr(model_admin, "mcp_exclude_fields", ())
    names = all_names
    if allow is not None:
        names = [n for n in names if n in allow]
    return [n for n in names if n not in deny]


def is_redacted(field_name: str) -> bool:
    """Substring match against REDACT_FIELDS. An explicit `mcp_fields` entry
    does not defeat redaction (SPEC.md section 7)."""
    name = field_name.lower()
    return any(marker in name for marker in conf.get("REDACT_FIELDS"))
