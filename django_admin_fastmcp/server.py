"""build_server() -> FastMCP (SPEC.md sections 4 and 12).

Eleven generic tools, mounted under the namespace "admin", so wire names are
"admin_list_models" and so on. The tool list is static and does not vary per
user. Sync `def` tools: FastMCP runs them in a worker thread, where Django's
sync ORM is legal (SPEC.md section 6).

With code mode on (CODEMODE.md), one more tool, admin_run_code, is async: it
runs no admin code itself, and the read tools it calls run in worker threads.
"""

from collections.abc import Callable

from fastmcp import FastMCP

from django_admin_fastmcp import conf
from django_admin_fastmcp.auth import build_auth, with_fresh_connections

MAX_BODY_BYTES = 256 * 1024
NAMESPACE = "admin"
# The catalogue name of admin_run_code, outside all_tools(): it exists only with code mode on.
CODEMODE_TOOL = "run_code"


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


def declared() -> dict[str, tuple[Callable, str]]:
    """The tools the ModelAdmins declare (tools/declared.py), keyed like `all_tools`."""
    from django_admin_fastmcp.tools.declared import declared_tools

    return declared_tools()


def build_server(*, with_auth: bool = True) -> FastMCP:
    """Assemble the FastMCP server.

    `with_auth=False` skips the bearer-token layer, for the in-memory test
    transport, which carries no HTTP headers. Never serve it over a network.
    """
    from django_admin_fastmcp.tools import codemode
    from django_admin_fastmcp.tracing import instrument

    disabled = set(conf.get("DISABLED_TOOLS"))
    with_codemode = codemode.is_enabled() and CODEMODE_TOOL not in disabled
    instructions = (
        "The Django admin of this project, as tools. Start with "
        "admin_list_models to see what you may touch, then "
        "admin_describe_model before reading or writing a model. "
        "Destructive tools preview first: call them with confirm=false, "
        "read the answer, then confirm."
    )
    server = FastMCP(
        name=conf.get("SERVER_NAME"),
        auth=build_auth() if with_auth else None,
        instructions=instructions + codemode.INSTRUCTIONS if with_codemode else instructions,
    )

    registered = {
        name: with_fresh_connections(instrument(name, fn))
        for name, fn in all_tools().items()
        if name not in disabled
    } | {
        name: with_fresh_connections(instrument(name, fn, model=model))
        for name, (fn, model) in declared().items()
        if name not in disabled
    }
    for name, fn in registered.items():
        server.tool(fn, name=f"{NAMESPACE}_{name}")

    if with_codemode:
        run_code, description = codemode.build_run_code(registered)
        server.tool(
            instrument(CODEMODE_TOOL, run_code),
            name=f"{NAMESPACE}_{CODEMODE_TOOL}",
            description=description,
            annotations={"readOnlyHint": True},
        )
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
