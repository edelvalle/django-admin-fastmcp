"""ToolError and the denial vocabulary (SPEC.md sections 3 and 5).

Fail closed: every unresolved lookup, missing ModelAdmin, unknown action,
unknown field, and unknown tool denies the call. `ToolError` is FastMCP's own
exception, so the message reaches the MCP client verbatim while everything
else stays masked.

Refusals and invalid calls raise `ToolRefusalError`, which FastMCP logs at INFO
instead of ERROR: they are not server faults, and an ERROR record would reach
Sentry through its logging integration.  A plain `ToolError` keeps the ERROR
level for faults the client cannot fix.
"""

import logging

from fastmcp.exceptions import ToolError

__all__ = ["ToolError", "ToolRefusalError", "denied", "not_found"]


class ToolRefusalError(ToolError):
    """A refusal or an invalid call, sent to the MCP client.  Not a server fault.

    FastMCP logs it at `log_level`, INFO by default.

    """

    def __init__(self, *args: object, log_level: int = logging.INFO) -> None:
        super().__init__(*args, log_level=log_level)


def denied(reason: str) -> ToolRefusalError:
    """A permission denial. Raise the return value."""
    return ToolRefusalError(f"denied: {reason}")


def not_found(label: str, pk: str) -> ToolRefusalError:
    """An object that does not exist or is hidden by get_queryset.

    One message for both cases, so a caller cannot probe for hidden rows.
    """
    return ToolRefusalError(f"not found: {label} with pk {pk!r}")
