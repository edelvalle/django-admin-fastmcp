"""Write and data-handling matrix (SPEC.md section 11)."""

import pytest
from demo.models import Book
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry

from django_admin_fastmcp.auth import impersonate
from django_admin_fastmcp.errors import ToolError
from django_admin_fastmcp.tools.read import search_objects
from django_admin_fastmcp.tools.write import create_object, delete_object, update_object


def test_create_goes_through_the_admin_and_logs_once(editor, author):
    with impersonate(editor):
        result = create_object(
            "demo.Book", {"title": "New", "author": str(author.pk), "published": True}
        )
    book = Book.objects.get(title="New")
    assert book.published is True
    assert result["pk"] == str(book.pk)
    # exactly one LogEntry, right user, right flag, client name in the message
    entry = LogEntry.objects.get(object_id=str(book.pk))
    assert entry.action_flag == ADDITION
    assert entry.user_id == editor.pk
    assert "Via MCP (client: test)" in entry.change_message
    # the save_model override's message_user reaches the caller
    assert any("through BookAdmin" in m for m in result["messages"])


def test_a_failed_write_leaves_no_row_and_no_log_entry(editor):
    with impersonate(editor), pytest.raises(ToolError, match="validation failed"):
        create_object("demo.Book", {"title": "Orphan"})  # author is required
    assert not Book.objects.filter(title="Orphan").exists()
    assert LogEntry.objects.count() == 0


def test_unknown_fields_are_an_error_not_a_no_op(editor, author):
    with impersonate(editor), pytest.raises(ToolError, match="unknown fields"):
        create_object(
            "demo.Book",
            {"title": "X", "author": str(author.pk), "no_such_field": 1},
        )


def test_a_submitted_readonly_field_is_ignored_not_written(editor, books):
    book = books["plain"]
    original_created_at = book.created_at
    with impersonate(editor):
        result = update_object(
            "demo.Book",
            str(book.pk),
            {"title": "Retitled", "created_at": "1999-01-01T00:00:00Z"},
        )
    book.refresh_from_db()
    assert book.title == "Retitled"
    assert book.created_at == original_created_at
    assert result["ignored_readonly_fields"] == ["created_at"]
    entry = LogEntry.objects.get(object_id=str(book.pk), action_flag=CHANGE)
    assert "title" in entry.change_message


def test_writes_refuse_models_outside_writable_models(superuser, credential):
    """ApiCredential is exposed and readable, never writable."""
    with impersonate(superuser), pytest.raises(ToolError, match="WRITABLE_MODELS"):
        update_object("demo.ApiCredential", str(credential.pk), {"name": "x"})


def test_delete_previews_by_default_and_the_cascade_is_exact(editor, author, books):
    with impersonate(editor):
        preview = delete_object("demo.Author", str(author.pk))
    assert preview["confirm"] is False
    assert preview["would_delete"]["counts"] == {"authors": 1, "books": 3}
    assert author.__class__.objects.filter(pk=author.pk).exists()  # nothing changed

    with impersonate(editor):
        result = delete_object("demo.Author", str(author.pk), confirm=True)
    assert result["confirm"] is True
    assert not author.__class__.objects.filter(pk=author.pk).exists()
    assert not Book.objects.exists()  # the cascade ran
    entry = LogEntry.objects.get(action_flag=DELETION, object_id=str(author.pk))
    assert entry.user_id == editor.pk


def test_unknown_filter_keys_error_instead_of_returning_everything(superuser, books):
    with impersonate(superuser):
        with pytest.raises(ToolError, match="unknown field"):
            search_objects("demo.Book", filters={"no_such_field": 1})
        with pytest.raises(ToolError, match="redacted"):
            search_objects("demo.ApiCredential", filters={"password__startswith": "h"})


def test_page_size_is_capped_not_rejected(superuser, books):
    with impersonate(superuser):
        result = search_objects("demo.Book", page_size=99999)
    assert result["page_size"] == 200  # MAX_PAGE_SIZE
    assert result["total"] == 3
