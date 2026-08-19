"""run_action (SPEC.md section 4).

Only actions from `ModelAdmin.get_actions(request)`, which already applies
each action's `allowed_permissions`. With `confirm=False` return the action
description and the affected count, and change nothing.
"""

from typing import Any

from django.db import transaction
from django.http import HttpResponse

from django_admin_fastmcp import conf
from django_admin_fastmcp.auth import current_user
from django_admin_fastmcp.errors import ToolError, denied
from django_admin_fastmcp.request import collected_messages
from django_admin_fastmcp.tools import require_write, resolve


def run_action(model: str, action: str, pks: list[str], confirm: bool = False) -> dict[str, Any]:
    """Run an admin action over a set of objects, with a mandatory preview.

    `action` must be one of the actions describe_model lists for you; the
    admin's own permission rules decide that list. `pks` selects the objects,
    all of which must be visible to you. With confirm=false (the default)
    nothing runs: the answer is the action's description and the affected
    count. Call again with confirm=true to run it. Refuses unknown actions,
    hidden objects, models not writable on this server, and read-only grants.
    """
    user = current_user()
    require_write(model)
    if len(pks) > conf.get("MAX_PKS"):
        raise ToolError(f"too many pks: {len(pks)} > MAX_PKS={conf.get('MAX_PKS')}")
    _, model_admin, request = resolve(user, model, "change")

    available = model_admin.get_actions(request)
    if action not in available:
        raise denied(f"action {action!r} is not available to you on {model}")
    entry = available[action]
    # Django 6.1+ exposes attributes; older versions hand out plain tuples.
    func = getattr(entry, "func", None) or entry[0]
    description = getattr(entry, "description", None) or entry[2]

    queryset = model_admin.get_queryset(request).filter(pk__in=pks)
    found = queryset.count()
    if found != len(set(pks)):
        raise ToolError(f"only {found} of {len(set(pks))} selected objects are visible to you")

    if not confirm:
        return {
            "confirm": False,
            "action": action,
            "description": str(description),
            "affected": found,
        }

    with transaction.atomic():
        response = func(model_admin, request, queryset)
    result: dict[str, Any] = {
        "confirm": True,
        "action": action,
        "affected": found,
        "messages": collected_messages(request),
    }
    if isinstance(response, HttpResponse):
        result["note"] = (
            "the action returned an HTTP response, which has no MCP equivalent and was discarded"
        )
    return result
