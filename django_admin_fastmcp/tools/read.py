"""search_objects, get_object, object_history, recent_actions (SPEC.md section 4).

Every read starts from `ModelAdmin.get_queryset(request)`, never from
`Model.objects.all()`. That single rule is what makes row-level overrides
apply for free.
"""

from typing import Any

from django.contrib.admin.models import LogEntry
from django.core.exceptions import FieldError, ValidationError
from django.db.models import Model

from django_admin_fastmcp import conf, exposure
from django_admin_fastmcp.auth import current_user
from django_admin_fastmcp.errors import ToolError, not_found
from django_admin_fastmcp.serialize import serialize_instance
from django_admin_fastmcp.tools import resolve, validate_lookup_path


def _get_visible(queryset, model: type[Model], pk: str):
    """Fetch one row from an admin queryset. Hidden and missing look the same."""
    label = exposure.label_of(model)
    try:
        return queryset.get(pk=pk)
    except (model.DoesNotExist, ValueError, ValidationError):
        raise not_found(label, pk) from None


def search_objects(
    model: str,
    q: str | None = None,
    filters: dict[str, Any] | None = None,
    order_by: list[str] | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    """Search rows of one model, as the admin changelist would.

    `model` is "app_label.ModelName". `q` runs the admin's own search over
    the model's search_fields. `filters` is a mapping of Django field lookups
    to values, for example {"published": true} or {"author__name": "Ada"}.
    An unknown field or lookup is an error, never a silent no-op. `order_by`
    is a list of field names, prefix "-" for descending. `page_size` is
    capped by the server. Returns {"rows": [...], "total": n, "page": n}.
    Rows you may not see, per the admin's own queryset, are absent.
    """
    user = current_user()
    model_class, model_admin, request = resolve(user, model, "view")
    queryset = model_admin.get_queryset(request)

    if q:
        queryset, may_have_duplicates = model_admin.get_search_results(request, queryset, q)
        if may_have_duplicates:
            queryset = queryset.distinct()
    if filters:
        for key in filters:
            validate_lookup_path(model_class, key)
        try:
            queryset = queryset.filter(**filters)
        except (FieldError, ValueError, ValidationError) as error:
            raise ToolError(f"invalid filters: {error}") from None
    if order_by:
        for key in order_by:
            validate_lookup_path(model_class, key)
        try:
            queryset = queryset.order_by(*order_by)
        except FieldError as error:
            raise ToolError(f"invalid order_by: {error}") from None

    page = max(page, 1)
    page_size = max(1, min(page_size, conf.get("MAX_PAGE_SIZE")))
    total = queryset.count()
    start = (page - 1) * page_size
    rows = [
        serialize_instance(obj, model_admin, request) for obj in queryset[start : start + page_size]
    ]
    return {"rows": rows, "total": total, "page": page, "page_size": page_size}


def get_object(model: str, pk: str) -> dict[str, Any]:
    """Read one object by primary key.

    `model` is "app_label.ModelName", `pk` is the primary key as a string.
    Returns the serialized instance: field values (sensitive fields read
    "[redacted]"), which fields are read-only for you, and a link into the
    real admin. Refuses objects the admin would not show you.
    """
    user = current_user()
    model_class, model_admin, request = resolve(user, model, "view")
    obj = _get_visible(model_admin.get_queryset(request), model_class, pk)
    return serialize_instance(obj, model_admin, request)


def _serialize_log_entry(entry: LogEntry) -> dict[str, Any]:
    return {
        "action_time": entry.action_time.isoformat(),
        "user": str(entry.user),
        "action": entry.get_action_flag_display(),
        "model": (
            f"{entry.content_type.app_label}.{entry.content_type.model}"
            if entry.content_type
            else None
        ),
        "object_id": entry.object_id,
        "object_repr": entry.object_repr,
        "change_message": entry.get_change_message(),
    }


def object_history(model: str, pk: str) -> list[dict[str, Any]]:
    """Read the admin history of one object, newest first.

    Returns the admin LogEntry rows: when, who, what action, and the change
    message. MCP edits appear here too, marked with the client name.
    """
    from django.contrib.contenttypes.models import ContentType

    user = current_user()
    model_class, model_admin, request = resolve(user, model, "view")
    obj = _get_visible(model_admin.get_queryset(request), model_class, pk)
    entries = (
        LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(model_class),
            object_id=str(obj.pk),
        )
        .select_related("user", "content_type")
        .order_by("-action_time")
    )
    return [_serialize_log_entry(entry) for entry in entries]


def recent_actions(limit: int = 25) -> list[dict[str, Any]]:
    """List recent admin actions, newest first.

    Shows your own actions. A superuser sees everyone's. `limit` is capped
    by the server.
    """
    user = current_user()
    limit = max(1, min(limit, conf.get("MAX_PAGE_SIZE")))
    entries = LogEntry.objects.select_related("user", "content_type").order_by("-action_time")
    if not user.is_superuser:
        entries = entries.filter(user=user)
    return [_serialize_log_entry(entry) for entry in entries[:limit]]
