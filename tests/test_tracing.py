"""The instrumentation wrapper (SPEC.md section 9, tracing.py).

One structured log record per call: tool, model, user, client, outcome,
duration. Denials log at WARNING. The wrapper must be transparent: same
signature for FastMCP's schema generation, same return value, same raised
exceptions.
"""

import inspect
import logging

import pytest

from django_admin_fastmcp.auth import impersonate
from django_admin_fastmcp.errors import ToolError
from django_admin_fastmcp.tools.read import get_object
from django_admin_fastmcp.tracing import instrument


def records(caplog):
    return [r for r in caplog.records if r.name == "django_admin_fastmcp"]


def test_a_successful_call_logs_one_info_record(caplog, superuser, books):
    wrapped = instrument("get_object", get_object)
    with caplog.at_level(logging.INFO, logger="django_admin_fastmcp"), impersonate(superuser):
        result = wrapped(model="demo.Book", pk=str(books["plain"].pk))
    assert result["fields"]["title"] == "Plain"
    (record,) = records(caplog)
    assert record.levelno == logging.INFO
    assert record.mcp_tool == "admin_get_object"
    assert record.mcp_model == "demo.Book"
    assert record.mcp_user == "root"
    assert record.mcp_outcome == "ok"
    assert record.mcp_duration_ms > 0


def test_a_denial_logs_warning_and_the_error_still_propagates(caplog, viewer, credential):
    wrapped = instrument("get_object", get_object)
    with caplog.at_level(logging.INFO, logger="django_admin_fastmcp"), impersonate(viewer):
        with pytest.raises(ToolError, match="no view permission"):
            wrapped(model="demo.ApiCredential", pk=str(credential.pk))
    (record,) = records(caplog)
    assert record.levelno == logging.WARNING
    assert record.mcp_outcome == "denied"


def test_an_unauthenticated_call_still_gets_a_record(caplog, db):
    wrapped = instrument("get_object", get_object)
    with caplog.at_level(logging.INFO, logger="django_admin_fastmcp"):
        with pytest.raises(ToolError, match="no authenticated user"):
            wrapped(model="demo.Book", pk="1")
    (record,) = records(caplog)
    assert record.mcp_user == "anonymous"
    assert record.mcp_outcome == "denied"


def test_a_bug_logs_outcome_error_and_reraises(caplog, superuser, db):
    def broken(model: str) -> dict:
        raise ValueError("boom")

    wrapped = instrument("broken", broken)
    with caplog.at_level(logging.INFO, logger="django_admin_fastmcp"), impersonate(superuser):
        with pytest.raises(ValueError, match="boom"):
            wrapped(model="demo.Book")
    (record,) = records(caplog)
    assert record.mcp_outcome == "error"


def test_the_wrapper_is_transparent_to_schema_generation():
    """FastMCP builds the input schema from the signature and the docstring.

    functools.wraps plus __wrapped__ keep both reachable, or every tool would
    advertise (*args, **kwargs) and no documentation.
    """
    wrapped = instrument("get_object", get_object)
    assert wrapped.__doc__ == get_object.__doc__
    assert str(inspect.signature(wrapped)) == str(inspect.signature(get_object))
