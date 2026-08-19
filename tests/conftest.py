"""Shared fixtures: users at every privilege level, and demo data."""

import pytest
from demo.models import ApiCredential, Author, Book
from django.contrib.auth.models import Permission, User


def grant(user: User, *perms: str) -> User:
    """Grant "app_label.codename" permissions and drop the permission cache."""
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )
    return User.objects.get(pk=user.pk)  # fresh instance, no cached perms


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("root", "root@example.com", "pw")


@pytest.fixture
def viewer(db):
    """Staff, may view Book and Author, nothing else."""
    user = User.objects.create_user("viewer", password="pw", is_staff=True)
    return grant(user, "demo.view_book", "demo.view_author")


@pytest.fixture
def editor(db):
    """Staff, full demo.Book and demo.Author permissions."""
    user = User.objects.create_user("editor", password="pw", is_staff=True)
    return grant(
        user,
        "demo.view_book",
        "demo.add_book",
        "demo.change_book",
        "demo.delete_book",
        "demo.view_author",
        "demo.add_author",
        "demo.change_author",
        "demo.delete_author",
        "demo.view_apicredential",
    )


@pytest.fixture
def book_editor(db):
    """Staff, change on Book only. Cannot change Author."""
    user = User.objects.create_user("book_editor", password="pw", is_staff=True)
    return grant(user, "demo.view_book", "demo.view_author", "demo.change_book")


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ada Lovelace", bio="Analyst.")


@pytest.fixture
def books(db, author):
    return {
        "plain": Book.objects.create(title="Plain", author=author),
        "hidden": Book.objects.create(title="Hidden", author=author, hidden=True),
        "locked": Book.objects.create(title="Locked", author=author, locked=True),
    }


@pytest.fixture
def credential(db):
    return ApiCredential.objects.create(
        name="prod", password="hunter2", api_key="k-123", notes="internal"
    )
