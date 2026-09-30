"""build_server() -> FastMCP (SPEC.md sections 4 and 12).

Eleven generic tools, mounted under the namespace "admin", so wire names are
"admin_list_models" and so on. The tool list is static and does not vary per
user. Sync `def` tools: FastMCP runs them in a worker thread, where Django's
sync ORM is legal (SPEC.md section 6).
"""

from collections.abc import Callable

from fastmcp import FastMCP

from django_admin_fastmcp import conf
from django_admin_fastmcp.auth import build_auth, with_fresh_connections

MAX_BODY_BYTES = 256 * 1024
NAMESPACE = "admin"


def all_tools() -> dict[str, Callable]:
    """The tool catalogue, keyed by the names DISABLED_TOOLS uses."""
    from django_admin_fastmcp.tools import actions, introspect, lookup, read, write

    return {
        "list_models": introspect.list_models,
        "describe_model": introspect.describe_model,
        "search_objects": read.search_objects,
        "get_object": read.get_object,
        "object_history": read.object_history,
        "recent_actions": read.recent_actions,
        "create_object": write.create_object,
        "update_object": write.update_object,
        "delete_object": write.delete_object,
        "run_action": actions.run_action,
        "autocomplete": lookup.autocomplete,
    }


def build_server(*, with_auth: bool = True) -> FastMCP:
    """Assemble the FastMCP server.

    `with_auth=False` skips the bearer-token layer, for the in-memory test
    transport, which carries no HTTP headers. Never serve it over a network.
    """
    server = FastMCP(
        name=conf.get("SERVER_NAME"),
        auth=build_auth() if with_auth else None,
        instructions=(
            "The Django admin of this project, as tools. Start with "
            "admin_list_models to see what you may touch, then "
            "admin_describe_model before reading or writing a model. "
            "Destructive tools preview first: call them with confirm=false, "
            "read the answer, then confirm."
        ),
    )
    from django_admin_fastmcp.tracing import instrument

    disabled = set(conf.get("DISABLED_TOOLS"))
    for name, fn in all_tools().items():
        if name not in disabled:
            server.tool(with_fresh_connections(instrument(name, fn)), name=f"{NAMESPACE}_{name}")
    return server


class BodySizeLimit:
    """Pure ASGI middleware: refuse request bodies over MAX_BODY_BYTES."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            length = headers.get(b"content-length")
            if length is not None and int(length) > MAX_BODY_BYTES:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"request body over 256 KiB",
                    }
                )
                return
        await self.app(scope, receive, send)
