"""Serialization matrix (SPEC.md sections 7 and 11)."""

from django_admin_fastmcp.auth import impersonate
from django_admin_fastmcp.tools.read import get_object


def test_redaction_wins_even_over_an_explicit_mcp_fields_entry(superuser, credential):
    """ApiCredentialAdmin.mcp_fields names the password on purpose."""
    with impersonate(superuser):
        data = get_object("demo.ApiCredential", str(credential.pk))
    assert data["fields"]["password"] == "[redacted]"
    assert data["fields"]["api_key"] == "[redacted]"
    assert data["fields"]["name"] == "prod"
    assert "notes" not in data["fields"]  # outside mcp_fields


def test_the_primary_key_is_always_present_as_a_string(superuser, books):
    """The model_to_dict failure both reference packages have."""
    with impersonate(superuser):
        data = get_object("demo.Book", str(books["plain"].pk))
    assert data["pk"] == str(books["plain"].pk)
    assert isinstance(data["pk"], str)
    assert data["fields"]["id"] == books["plain"].pk


def test_foreign_keys_carry_a_label_not_just_a_pk(superuser, books, author):
    with impersonate(superuser):
        data = get_object("demo.Book", str(books["plain"].pk))
    assert data["fields"]["author"] == {"pk": str(author.pk), "label": "Ada Lovelace"}


def test_readonly_fields_are_flagged_and_admin_url_present(superuser, books):
    with impersonate(superuser):
        data = get_object("demo.Book", str(books["plain"].pk))
    assert "created_at" in data["readonly_fields"]
    assert data["admin_url"] == f"/admin/demo/book/{books['plain'].pk}/change/"
    # dates serialize as ISO 8601 strings
    assert "T" in data["fields"]["created_at"]
