"""Authorization matrix (SPEC.md sections 5 and 11)."""

import asyncio

import pytest

from django_admin_fastmcp.auth import impersonate
from django_admin_fastmcp.errors import ToolError
from django_admin_fastmcp.server import build_server
from django_admin_fastmcp.tools.actions import run_action
from django_admin_fastmcp.tools.introspect import describe_model, list_models
from django_admin_fastmcp.tools.read import get_object, search_objects
from django_admin_fastmcp.tools.write import create_object, delete_object, update_object


def labels(user) -> set[str]:
    with impersonate(user):
        return {entry["model"] for entry in list_models()}


def test_a_superuser_reaches_every_exposed_model(superuser):
    exposed = labels(superuser)
    assert {"demo.Book", "demo.Author", "demo.ApiCredential", "auth.User"} <= exposed
    assert "demo.SecretProject" not in exposed
    assert "demo.UnregisteredThing" not in exposed
    assert not any(label.startswith("django_admin_fastmcp.") for label in exposed)


def test_list_models_differs_per_user(superuser, viewer):
    assert labels(viewer) == {"demo.Book", "demo.Author"}
    assert labels(viewer) < labels(superuser)


def test_view_only_staff_can_read_but_not_write(viewer, books, author):
    with impersonate(viewer):
        assert search_objects("demo.Book")["total"] == 2  # hidden row absent
        get_object("demo.Book", str(books["plain"].pk))
        with pytest.raises(ToolError, match="no add permission"):
            create_object("demo.Book", {"title": "X", "author": str(author.pk)})
        with pytest.raises(ToolError, match="no change permission"):
            update_object("demo.Book", str(books["plain"].pk), {"title": "X"})
        with pytest.raises(ToolError, match="no delete permission"):
            delete_object("demo.Book", str(books["plain"].pk))


def test_change_on_model_a_does_not_leak_to_model_b(book_editor, books, author):
    with impersonate(book_editor):
        update_object("demo.Book", str(books["plain"].pk), {"title": "Renamed"})
        with pytest.raises(ToolError, match="no change permission"):
            update_object("demo.Author", str(author.pk), {"name": "X"})
    books["plain"].refresh_from_db()
    assert books["plain"].title == "Renamed"


def test_get_queryset_override_hides_rows_everywhere(editor, superuser, books):
    hidden_pk = str(books["hidden"].pk)
    with impersonate(editor):
        rows = search_objects("demo.Book")
        assert all(row["fields"]["title"] != "Hidden" for row in rows["rows"])
        with pytest.raises(ToolError, match="not found"):
            get_object("demo.Book", hidden_pk)
        with pytest.raises(ToolError, match="not found"):
            update_object("demo.Book", hidden_pk, {"title": "X"})
    with impersonate(superuser):  # the override lets superusers through
        assert get_object("demo.Book", hidden_pk)["fields"]["title"] == "Hidden"


def test_object_level_has_change_permission(editor, books):
    with impersonate(editor):
        update_object("demo.Book", str(books["plain"].pk), {"title": "Fine"})
        with pytest.raises(ToolError, match="no change permission on this"):
            update_object("demo.Book", str(books["locked"].pk), {"title": "Nope"})
    books["locked"].refresh_from_db()
    assert books["locked"].title == "Locked"


def test_run_action_refuses_actions_outside_get_actions(viewer, editor, books):
    pks = [str(books["plain"].pk)]
    # no change permission, so the action is filtered out for the viewer
    with impersonate(viewer), pytest.raises(ToolError):
        run_action("demo.Book", "publish_books", pks)
    with impersonate(editor), pytest.raises(ToolError, match="not available"):
        run_action("demo.Book", "no_such_action", pks)


def test_unexposed_models_are_refused(superuser):
    with impersonate(superuser):
        for label in (
            "demo.UnregisteredThing",  # never registered in the admin
            "demo.SecretProject",  # mcp_expose = False
            "auth.Permission",  # EXCLUDE_MODELS
            "django_admin_fastmcp.McpToken",  # built-in denylist
            "no.SuchModel",
        ):
            with pytest.raises(ToolError, match="not exposed"):
                describe_model(label)


def test_default_permissions_apply_without_overrides(viewer, superuser, credential):
    # AuthorAdmin has no overrides; ApiCredential needs its own view permission.
    with impersonate(viewer), pytest.raises(ToolError, match="no view permission"):
        get_object("demo.ApiCredential", str(credential.pk))
    with impersonate(superuser):
        get_object("demo.ApiCredential", str(credential.pk))


@pytest.mark.django_db
def test_the_wire_catalogue_is_static_per_user(superuser, viewer):
    """tools/list does not vary per user; list_models does (tested above)."""
    from fastmcp import Client

    async def tool_names():
        async with Client(build_server(with_auth=False)) as client:
            return {tool.name for tool in await client.list_tools()}

    with impersonate(superuser):
        as_superuser = asyncio.run(tool_names())
    with impersonate(viewer):
        as_viewer = asyncio.run(tool_names())
    assert as_superuser == as_viewer
    assert "admin_list_models" in as_superuser
    assert len(as_superuser) == 11


# transaction=True: the MCP client runs the sync tool in a worker thread,
# whose DB connection cannot see this test's uncommitted transaction.
@pytest.mark.django_db(transaction=True)
def test_calls_work_through_a_real_mcp_client(superuser, books):
    from fastmcp import Client

    async def call():
        async with Client(build_server(with_auth=False)) as client:
            result = await client.call_tool(
                "admin_search_objects", {"model": "demo.Book", "q": "Plain"}
            )
            return result.data

    with impersonate(superuser):
        data = asyncio.run(call())
    assert data["total"] == 1
    assert data["rows"][0]["fields"]["title"] == "Plain"


@pytest.mark.django_db
def test_disabled_tools_vanish_from_the_catalogue(settings, superuser):
    from fastmcp import Client

    settings.ADMIN_FASTMCP = {**settings.ADMIN_FASTMCP, "DISABLED_TOOLS": ("delete_object",)}

    async def tool_names():
        async with Client(build_server(with_auth=False)) as client:
            return {tool.name for tool in await client.list_tools()}

    with impersonate(superuser):
        names = asyncio.run(tool_names())
    assert "admin_delete_object" not in names
    assert len(names) == 10
