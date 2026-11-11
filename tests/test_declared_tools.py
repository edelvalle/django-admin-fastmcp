"""Declared tools: a ModelAdmin method as an MCP tool (SPEC.md section 4)."""

import asyncio

import pytest
from demo.admin import BookAdmin
from demo.models import Book
from django.contrib.admin.models import CHANGE, LogEntry
from fastmcp import Client

from django_admin_fastmcp.auth import impersonate
from django_admin_fastmcp.errors import ToolError
from django_admin_fastmcp.server import all_tools, build_server, declared
from django_admin_fastmcp.tools.introspect import describe_model
from django_admin_fastmcp.tools.write import create_object


def test_the_catalogue_names_the_tool_after_the_model():
    assert set(declared()) == {"demo_book_retitle", "demo_book_count_books"}
    assert declared()["demo_book_retitle"][1] == "demo.Book"
    assert "demo_book_retitle" not in all_tools()  # the generic catalogue stays as it is


def test_describe_model_lists_the_tools_the_caller_may_run(editor, viewer):
    with impersonate(editor):
        tools = describe_model("demo.Book")["tools"]
    assert tools == {
        "admin_demo_book_retitle": "Give a book a new title.",
        "admin_demo_book_count_books": "How many books the caller can see, published or not.",
    }
    with impersonate(viewer):
        tools = describe_model("demo.Book")["tools"]
    assert list(tools) == ["admin_demo_book_count_books"]


def test_a_tool_runs_as_the_caller_logs_and_returns_the_row(editor, books):
    book = books["plain"]
    retitle = declared()["demo_book_retitle"][0]
    with impersonate(editor):
        result = retitle(pk=str(book.pk), title="Renamed")
    book.refresh_from_db()
    assert book.title == "Renamed"
    assert result["pk"] == str(book.pk)
    assert result["fields"]["title"] == "Renamed"
    assert any("through BookAdmin" in m for m in result["messages"])
    entry = LogEntry.objects.get(object_id=str(book.pk))
    assert entry.action_flag == CHANGE
    assert entry.user_id == editor.pk
    assert "Ran retitle" in entry.change_message


def test_a_read_tool_needs_no_write_gate(viewer, books, settings):
    settings.ADMIN_FASTMCP = {**settings.ADMIN_FASTMCP, "WRITABLE_MODELS": ()}
    count_books = declared()["demo_book_count_books"][0]
    with impersonate(viewer):
        assert count_books(published=True)["count"] == Book.objects.filter(published=True).count()


def test_a_write_tool_is_refused_without_change_permission(viewer, books):
    retitle = declared()["demo_book_retitle"][0]
    with impersonate(viewer), pytest.raises(ToolError, match="no change permission"):
        retitle(pk=str(books["plain"].pk), title="Nope")


def test_a_write_tool_is_refused_when_the_model_is_not_writable(editor, books, settings):
    settings.ADMIN_FASTMCP = {**settings.ADMIN_FASTMCP, "WRITABLE_MODELS": ()}
    retitle = declared()["demo_book_retitle"][0]
    with impersonate(editor), pytest.raises(ToolError, match="WRITABLE_MODELS"):
        retitle(pk=str(books["plain"].pk), title="Nope")


def test_a_validation_error_is_a_tool_error_and_rolls_back(editor, books):
    retitle = declared()["demo_book_retitle"][0]
    with impersonate(editor), pytest.raises(ToolError, match="validation failed"):
        retitle(pk=str(books["plain"].pk), title="   ")
    assert LogEntry.objects.count() == 0


def test_the_method_keeps_its_own_row_level_rules(editor, books):
    retitle = declared()["demo_book_retitle"][0]
    with impersonate(editor), pytest.raises(PermissionError):
        retitle(pk=str(books["locked"].pk), title="Nope")


def test_generic_writes_can_be_turned_off_per_model(editor, author, monkeypatch):
    monkeypatch.setattr(BookAdmin, "mcp_generic_writes", False, raising=False)
    with impersonate(editor), pytest.raises(ToolError, match="declared tools"):
        create_object("demo.Book", {"title": "New", "author": str(author.pk)})


def test_disabled_tools_covers_declared_tools(settings):
    settings.ADMIN_FASTMCP = {**settings.ADMIN_FASTMCP, "DISABLED_TOOLS": ("demo_book_retitle",)}
    names = {t.name for t in asyncio.run(_list_tools())}
    assert "admin_demo_book_count_books" in names
    assert "admin_demo_book_retitle" not in names


def test_fastmcp_builds_the_schema_from_the_method_signature():
    """The input schema comes from the method's own parameters, through the tracing and
    connection wrappers.  The run itself is covered above: the in-memory client calls
    the tool in a worker thread, where the test impersonation does not reach."""
    tools = {t.name: t for t in asyncio.run(_list_tools())}
    schema = tools["admin_demo_book_retitle"].inputSchema
    assert set(schema["properties"]) == {"pk", "title"}
    assert set(schema["required"]) == {"pk", "title"}
    assert tools["admin_demo_book_retitle"].description == "Give a book a new title."
    count = tools["admin_demo_book_count_books"].inputSchema
    # `datetime` is imported only in the demo admin, which postpones its annotations:
    # the hint resolves there, not in the library's module.
    assert set(count["properties"]) == {"published", "created_after"}
    assert {"type": "string", "format": "date-time"} in count["properties"]["created_after"]["anyOf"]
    assert "required" not in count or count["required"] == []


async def _list_tools():
    async with Client(build_server(with_auth=False)) as client:
        return await client.list_tools()
