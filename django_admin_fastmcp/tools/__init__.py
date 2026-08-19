"""Shared resolution for every tool (SPEC.md section 5).

Each tool resolves its target once, through `resolve`. Denies, in order: an
inactive user, a non-staff user, a hidden model, an unregistered model, and a
ModelAdmin that refuses the action. Every failure path denies. No branch
allows a call because a lookup returned None.
"""

from typing import Literal

from django.apps import apps
from django.contrib.admin import ModelAdmin
from django.db.models import Model
from django.http import HttpRequest

from django_admin_fastmcp import exposure
from django_admin_fastmcp.auth import current_scopes
from django_admin_fastmcp.errors import ToolError, denied
from django_admin_fastmcp.models import SCOPE_WRITE
from django_admin_fastmcp.request import admin_request

Action = Literal["view", "add", "change", "delete"]


def resolve(user, label: str, action: Action) -> tuple[type[Model], ModelAdmin, HttpRequest]:
    """Return the model, its ModelAdmin, and a synthetic request, or raise."""
    if not (user.is_active and user.is_staff):
        raise denied("not a staff user")
    if not exposure.is_exposed(label):
        raise ToolError(f"model {label} is not exposed")
    model = apps.get_model(label)  # is_exposed already resolved it
    model_admin = exposure.admin_site()._registry.get(model)
    if model_admin is None:
        raise ToolError(f"model {label} is not registered in the admin")
    request = admin_request(user, model_admin, method="GET" if action == "view" else "POST")
    if not getattr(model_admin, f"has_{action}_permission")(request):
        raise denied(f"no {action} permission on {label}")
    return model, model_admin, request


def require_write(label: str) -> None:
    """The two write gates on top of ModelAdmin permissions (SPEC.md section 9)."""
    if SCOPE_WRITE not in current_scopes():
        raise denied("this grant lacks the admin:write scope")
    if not exposure.is_writable(label):
        raise denied(f"model {label} is not in WRITABLE_MODELS")


def validate_lookup_path(model: type[Model], lookup: str) -> None:
    """A filter or ordering key must start at a real concrete field, and no
    segment may name a redacted field. Unknown is an error, never a no-op."""
    from django_admin_fastmcp.serialize import concrete_fields

    segments = lookup.removeprefix("-").split("__")
    field_names = {f.name for f in concrete_fields(model)}
    if segments[0] not in field_names:
        raise ToolError(f"unknown field {segments[0]!r} on {exposure.label_of(model)}")
    for segment in segments:
        if exposure.is_redacted(segment):
            raise denied(f"cannot filter or order by redacted field {segment!r}")
