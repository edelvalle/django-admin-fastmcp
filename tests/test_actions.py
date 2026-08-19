"""Action matrix (SPEC.md sections 4 and 11)."""

import pytest
from demo.models import Book

from django_admin_fastmcp.auth import impersonate
from django_admin_fastmcp.errors import ToolError
from django_admin_fastmcp.tools.actions import run_action


def test_actions_preview_by_default(editor, books):
    pks = [str(books["plain"].pk), str(books["locked"].pk)]
    with impersonate(editor):
        preview = run_action("demo.Book", "publish_books", pks)
    assert preview == {
        "confirm": False,
        "action": "publish_books",
        "description": "Publish selected books",
        "affected": 2,
    }
    assert Book.objects.filter(published=True).count() == 0


def test_confirmed_actions_run_and_report_their_messages(editor, books):
    pks = [str(books["plain"].pk), str(books["locked"].pk)]
    with impersonate(editor):
        result = run_action("demo.Book", "publish_books", pks, confirm=True)
    assert result["affected"] == 2
    assert "Published 2 book(s)." in result["messages"]
    assert Book.objects.filter(published=True).count() == 2


def test_hidden_rows_cannot_be_reached_through_pks(editor, books):
    with impersonate(editor), pytest.raises(ToolError, match="visible"):
        run_action(
            "demo.Book",
            "publish_books",
            [str(books["plain"].pk), str(books["hidden"].pk)],
            confirm=True,
        )
    assert Book.objects.filter(published=True).count() == 0


def test_pks_over_max_pks_are_refused(settings, editor, books):
    settings.ADMIN_FASTMCP = {**settings.ADMIN_FASTMCP, "MAX_PKS": 2}
    with impersonate(editor), pytest.raises(ToolError, match="MAX_PKS"):
        run_action("demo.Book", "publish_books", ["1", "2", "3"])


def test_a_read_only_scope_cannot_run_actions(editor, books):
    with impersonate(editor, scopes=["admin:read"]), pytest.raises(ToolError, match="admin:write"):
        run_action("demo.Book", "publish_books", [str(books["plain"].pk)])
