"""Code mode: admin_run_code runs a snippet that calls the read tools (CODEMODE.md)."""

import asyncio
import logging

from fastmcp import Client

from django_admin_fastmcp.auth import impersonate
from django_admin_fastmcp.server import build_server

SNIPPET = """\
import asyncio

# books Ada wrote, with their history
page = await search_objects(model="demo.Book", q="")
histories = await asyncio.gather(
    *(object_history(model="demo.Book", pk=row["pk"]) for row in page["rows"])
)
sorted(row["fields"]["title"] for row, history in zip(page["rows"], histories) if not history)
"""


def test_a_snippet_calls_the_read_tools_as_the_caller(
    transactional_db, viewer, books, settings, caplog
):
    """The inner calls run as the caller, in worker threads: the viewer does not see the
    hidden book, as with a direct call.  The run's audit record keeps the snippet without
    its data, and counts the calls."""
    settings.ADMIN_FASTMCP = {**settings.ADMIN_FASTMCP, "CODEMODE_ENABLED": True}

    async def run_snippet():
        async with Client(build_server(with_auth=False)) as client:
            return await client.call_tool("admin_run_code", {"code": SNIPPET})

    with caplog.at_level(logging.INFO, logger="django_admin_fastmcp"), impersonate(viewer):
        result = asyncio.run(run_snippet())

    assert result.structured_content == {"result": ["Locked", "Plain"], "stdout": ""}
    (record,) = [r for r in caplog.records if r.mcp_tool == "admin_run_code"]
    assert record.mcp_user == "viewer"
    assert record.mcp_outcome == "ok"
    assert record.mcp_calls == 3
    assert "q='[redacted]'" in record.mcp_snippet
    assert "model='demo.Book'" in record.mcp_snippet
    assert "Ada" not in record.mcp_snippet
