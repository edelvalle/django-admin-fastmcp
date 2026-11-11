"""Tools a ModelAdmin declares (SPEC.md section 4, "Declared tools").

The generic tools write rows through the admin form. Some models do not change that
way: a tree node is created through its parent, an aggregate only through its events.
A `ModelAdmin` declares a tool for each such operation with `@mcp_tool`, next to the
model it acts on, the way `@admin.action` declares an action:

    @admin.register(Order)
    class OrderAdmin(admin.ModelAdmin):
        mcp_generic_writes = False  # rows change only through the tools below

        @mcp_tool(write=True)
        def add_line(self, request, order_pk: str, sku: str, quantity: int = 1) -> Order:
            '''Add a line to an order.'''
            order = self.get_queryset(request).get(pk=order_pk)
            order.lines.create(sku=sku, quantity=quantity)
            return order

The method takes `self`, the synthetic admin `request`, then its own typed parameters,
which become the tool's input schema. Its docstring is the description the agent reads.
It returns a model instance, serialized like `get_object` and logged as a change, a
JSON-able dict, or None.

A tool reads by default and needs the view permission on the model. A tool declared
with `write=True` has the gates of a generic write: the model in `WRITABLE_MODELS` and
the change permission. Both need a staff user and an exposed model. The call runs in a
transaction, so a method that raises leaves nothing behind. A Django `ValidationError`
becomes a tool error with its messages. Row-level checks are the method's own:
`self.get_queryset(request)` hides what the caller may not see, and
`self.has_change_permission(request, obj)` refuses what the caller may not change.

The wire name is `admin_<app_label>_<model_name>_<tool name>`; `DISABLED_TOOLS` names it
as `<app_label>_<model_name>_<tool name>`.
"""

import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.contrib.admin import ModelAdmin
from django.contrib.admin.models import CHANGE
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Model

from django_admin_fastmcp import exposure
from django_admin_fastmcp.auth import current_user
from django_admin_fastmcp.errors import ToolError
from django_admin_fastmcp.request import collected_messages
from django_admin_fastmcp.serialize import serialize_instance
from django_admin_fastmcp.tools import Action, require_write, resolve

ATTRIBUTE = "_mcp_tool"


@dataclass(frozen=True)
class ToolMeta:
    name: str
    description: str
    write: bool

    @property
    def permission(self) -> Action:
        """The admin permission the caller needs: change for a write, view for a read."""
        return "change" if self.write else "view"


def mcp_tool(
    fn: Callable | None = None,
    *,
    write: bool = False,
    name: str | None = None,
) -> Callable:
    """Mark a ModelAdmin method as an MCP tool.

    A tool reads by default: the caller needs the view permission on the model. Pass
    `write=True` for a tool that changes data: the model must be in `WRITABLE_MODELS`
    and the caller needs the change permission. `name` defaults to the method name.
    The docstring is the description the agent reads: say what the tool does, what the
    parameters mean, and what it refuses.
    """

    def decorate(method: Callable) -> Callable:
        setattr(
            method,
            ATTRIBUTE,
            ToolMeta(
                name=name or method.__name__,
                description=inspect.getdoc(method) or "",
                write=write,
            ),
        )
        return method

    return decorate(fn) if fn is not None else decorate


def declared_tools() -> dict[str, tuple[Callable, str]]:
    """The declared tools of every exposed model: catalogue name -> (tool, model label)."""
    tools: dict[str, tuple[Callable, str]] = {}
    for model, model_admin in exposure.exposed_models():
        label = exposure.label_of(model)
        opts = model._meta
        for method, meta in tools_of(model_admin):
            key = f"{opts.app_label}_{opts.model_name}_{meta.name}"
            tools[key] = (_bind(label, method, meta), label)
    return tools


def tools_of(model_admin: ModelAdmin) -> list[tuple[Callable, ToolMeta]]:
    """The `(method, meta)` pairs a ModelAdmin declares, in definition order."""
    found: list[tuple[Callable, ToolMeta]] = []
    for attribute in dir(type(model_admin)):
        method = getattr(type(model_admin), attribute, None)
        meta = getattr(method, ATTRIBUTE, None)
        if callable(method) and isinstance(meta, ToolMeta):
            found.append((method, meta))
    return found


def available_tools(model_admin: ModelAdmin, request) -> dict[str, str]:
    """Wire name -> description of the declared tools this caller may run on the model."""
    opts = model_admin.model._meta
    return {
        f"admin_{opts.app_label}_{opts.model_name}_{meta.name}": meta.description
        for _, meta in tools_of(model_admin)
        if getattr(model_admin, f"has_{meta.permission}_permission")(request)
    }


def _bind(label: str, method: Callable, meta: ToolMeta) -> Callable:
    """The tool function FastMCP registers: the method's own parameters, the gates
    of a write around the call."""
    # Resolve the hints in the module of the method: with postponed annotations they
    # are strings, and the wrapper lives in this module, where those names do not exist.
    hints = typing.get_type_hints(method)
    signature = inspect.signature(method)
    parameters = [
        parameter.replace(annotation=hints.get(parameter.name, parameter.annotation))
        for parameter in list(signature.parameters.values())[2:]  # drop self and request
    ]

    def tool(**kwargs: Any) -> Any:
        user = current_user()
        if meta.write:
            require_write(label)
        _, model_admin, request = resolve(user, label, meta.permission)
        try:
            with transaction.atomic():
                result = method(model_admin, request, **kwargs)
                if isinstance(result, Model):
                    _log_change(request, result, meta.name)
        except ValidationError as error:
            raise ToolError(f"validation failed: {_messages(error)}") from None
        if isinstance(result, Model):
            payload: dict[str, Any] = serialize_instance(result, model_admin, request)
        elif result is None:
            payload = {}
        elif isinstance(result, dict):
            payload = dict(result)
        else:
            raise ToolError(
                f"tool {meta.name} returned {type(result).__name__}; "
                "return a model instance, a dict, or None"
            )
        payload["messages"] = collected_messages(request)
        return payload

    # FastMCP reads the name, the docstring, the signature and the annotations to build
    # the tool and its input schema: those of the method, without self and request.  No
    # return annotation: the method may return a model instance, which has no JSON
    # schema, and the tool always answers with a dict.
    wrapper: Any = tool
    wrapper.__name__ = wrapper.__qualname__ = meta.name
    wrapper.__doc__ = meta.description
    wrapper.__signature__ = signature.replace(
        parameters=parameters, return_annotation=inspect.Signature.empty
    )
    annotations = {
        name: hint for name, hint in hints.items() if name not in ("self", "request", "return")
    }
    wrapper.__annotations__ = annotations
    # Python 3.14 evaluates annotations lazily through `__annotate__`, and that is what
    # `functools.wraps` carries to the tracing and connection wrappers around this
    # function, so it must answer with the same mapping.  Older Pythons copy
    # `__annotations__` and have no `__annotate__` to set.
    try:
        wrapper.__annotate__ = lambda format: dict(annotations)
    except AttributeError:
        pass
    return wrapper


def _log_change(request, obj: Model, tool_name: str) -> None:
    # Local import: write imports read, which imports this package's helpers.
    from django_admin_fastmcp.tools.write import _log, _via_mcp

    _log(request, obj, CHANGE, _via_mcp(f"Ran {tool_name}."))


def _messages(error: ValidationError) -> Any:
    if hasattr(error, "message_dict"):
        return error.message_dict
    else:
        return error.messages
