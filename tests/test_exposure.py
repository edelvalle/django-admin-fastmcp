"""Exposure matrix (SPEC.md sections 7 and 11)."""

from django_admin_fastmcp import exposure


def test_the_builtin_denylist_cannot_be_overridden(settings, db):
    settings.ADMIN_FASTMCP = {
        **settings.ADMIN_FASTMCP,
        "MODELS": ("django_admin_fastmcp.McpToken",),  # explicit allowlisting
        "EXCLUDE_MODELS": (),
    }
    assert not exposure.is_exposed("django_admin_fastmcp.McpToken")
    assert not exposure.is_exposed("django_admin_fastmcp.McpClient")
    assert not exposure.is_exposed("sessions.Session")


def test_the_models_allowlist_excludes_everything_else(settings, db):
    settings.ADMIN_FASTMCP = {**settings.ADMIN_FASTMCP, "MODELS": ("demo.Book",)}
    assert exposure.is_exposed("demo.Book")
    assert not exposure.is_exposed("demo.Author")
    assert not exposure.is_exposed("auth.User")


def test_exclude_models_supports_app_wildcards(settings, db):
    settings.ADMIN_FASTMCP = {**settings.ADMIN_FASTMCP, "EXCLUDE_MODELS": ("demo.*",)}
    assert not exposure.is_exposed("demo.Book")
    assert not exposure.is_exposed("demo.Author")
    assert exposure.is_exposed("auth.User")


def test_model_names_are_case_insensitive(db):
    assert exposure.is_exposed("demo.book")
    assert exposure.is_exposed("demo.BOOK")


def test_unresolvable_labels_fail_closed(db):
    assert not exposure.is_exposed("nope")
    assert not exposure.is_exposed("nope.Nope")
    assert not exposure.is_exposed("")


def test_mcp_expose_false_and_unregistered_are_hidden(db):
    assert not exposure.is_exposed("demo.SecretProject")
    assert not exposure.is_exposed("demo.UnregisteredThing")


def test_writable_requires_exposure_and_the_allowlist(settings, db):
    assert exposure.is_writable("demo.Book")
    assert not exposure.is_writable("demo.ApiCredential")  # exposed, not writable
    settings.ADMIN_FASTMCP = {
        **settings.ADMIN_FASTMCP,
        "WRITABLE_MODELS": ("demo.SecretProject",),  # writable but not exposed
    }
    assert not exposure.is_writable("demo.SecretProject")
