"""ToolError and the denial vocabulary (SPEC.md sections 3 and 5).

Fail closed: every unresolved lookup, missing ModelAdmin, unknown action,
unknown field, and unknown tool denies the call. `ToolError` is FastMCP's own
exception, so the message reaches the MCP client verbatim while everything
else stays masked.
"""

from fastmcp.exceptions import ToolError

__all__ = ["ToolError", "denied", "not_found"]


def denied(reason: str) -> ToolError:
    """A permission denial. Raise the return value."""
    return ToolError(f"denied: {reason}")


def not_found(label: str, pk: str) -> ToolError:
    """An object that does not exist or is hidden by get_queryset.

    One message for both cases, so a caller cannot probe for hidden rows.
    """
    return ToolError(f"not found: {label} with pk {pk!r}")
