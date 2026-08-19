"""instance -> dict, from admin metadata (SPEC.md section 7).

Never `model_to_dict`. It drops non-editable fields, including the primary
key, and it includes every editable field, including password hashes.

Serialization walks `model._meta.get_fields()` and produces concrete fields
by value, the primary key always as a string, foreign keys as
`{"pk": ..., "label": str(obj)}`, many-to-many as a capped and counted list
of the same shape, readonly fields flagged, redacted values as
`"[redacted]"`, and an `admin_url`. Reverse relations are not expanded.
"""

import datetime
import decimal
import uuid
from typing import Any

from django.contrib.admin import ModelAdmin
from django.db.models import Field, ForeignKey, ManyToManyField, Model
from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse

REDACTED = "[redacted]"
M2M_CAP = 20


def scalar(value: Any) -> Any:
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal | uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _related_shape(obj: Model | None) -> dict | None:
    if obj is None:
        return None
    return {"pk": str(obj.pk), "label": str(obj)}


def admin_url(instance: Model) -> str | None:
    opts = instance._meta
    try:
        return reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[instance.pk])
    except NoReverseMatch:
        return None


def concrete_fields(model: type[Model]) -> list[Field]:
    return [f for f in model._meta.get_fields() if isinstance(f, Field)]


def serialize_instance(
    instance: Model,
    model_admin: ModelAdmin,
    request: HttpRequest,
) -> dict[str, Any]:
    from django_admin_fastmcp import exposure

    model = type(instance)
    fields = concrete_fields(model)
    names = exposure.visible_field_names(model_admin, [f.name for f in fields])
    readonly = set(model_admin.get_readonly_fields(request, instance))

    data: dict[str, Any] = {}
    for field in fields:
        if field.name not in names:
            continue
        if exposure.is_redacted(field.name):
            data[field.name] = REDACTED
        elif isinstance(field, ManyToManyField):
            related = getattr(instance, field.name).all()
            total = related.count()
            data[field.name] = {
                "items": [_related_shape(obj) for obj in related[:M2M_CAP]],
                "total": total,
            }
        elif isinstance(field, ForeignKey):
            data[field.name] = _related_shape(getattr(instance, field.name))
        else:
            data[field.name] = scalar(getattr(instance, field.name))

    return {
        "pk": str(instance.pk),
        "fields": data,
        "readonly_fields": sorted(readonly & set(names)),
        "admin_url": admin_url(instance),
        "str": str(instance),
    }
