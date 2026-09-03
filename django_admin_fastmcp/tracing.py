"""Optional observability: spans, metrics, and one log record per tool call.

Mirrors djhtmx's tracing module. Sentry and Logfire are optional extras
(`django-admin-fastmcp[sentry]`, `[logfire]`); without them, or with the
ENABLE_*_TRACING settings off, everything here is a cheap no-op. The
structured log record per call is SPEC.md section 9 and always emits:
tool, model, user, client, outcome, and duration. Denials log at WARNING.

What reaches Sentry per tool call:

- a transaction (or child span, when the ASGI integration already opened
  one) named after the wire tool, op "mcp.tool",
- tags: mcp.tool, mcp.model, mcp.outcome, mcp.client,
- the calling Django user as the Sentry user,
- a counter per tool and outcome, and a duration distribution per tool,
- the exception, for outcomes that are bugs rather than denials.
"""

import contextlib
import functools
import logging
import time
from collections.abc import Callable
from typing import Any

from django_admin_fastmcp import conf
from django_admin_fastmcp.errors import ToolError

try:
    import sentry_sdk
except ImportError:  # pragma: no cover - exercised implicitly when absent
    sentry_sdk = None  # type: ignore[assignment]

try:
    import logfire
except ImportError:  # pragma: no cover
    logfire = None  # type: ignore[assignment]

logger = logging.getLogger("django_admin_fastmcp")


def _sentry_on() -> bool:
    return sentry_sdk is not None and conf.get("ENABLE_SENTRY_TRACING")


def _logfire_on() -> bool:
    if logfire is None or not conf.get("ENABLE_LOGFIRE_TRACING"):
        return False
    # Installed is not enough: an unconfigured logfire warns loudly on every
    # span. Stay off until the consumer has called logfire.configure().
    config = logfire.DEFAULT_LOGFIRE_INSTANCE.config
    return bool(getattr(config, "_initialized", False))


# -- spans -------------------------------------------------------------------


@contextlib.contextmanager
def _sentry_span(name: str, tags: dict[str, str]):
    if not _sentry_on():
        yield None
        return
    # Under an ASGI/Django integration a transaction is already open, so nest
    # a span. Standalone (the admin_mcp_serve process without an integration)
    # there is none, so open a transaction or the span records nothing.
    if sentry_sdk.get_current_span() is not None:
        context = sentry_sdk.start_span(op="mcp.tool", name=name)
    else:
        context = sentry_sdk.start_transaction(op="mcp.tool", name=name)
    with context as span:
        for key, value in tags.items():
            span.set_tag(key, value)
        yield span


@contextlib.contextmanager
def _logfire_span(name: str, tags: dict[str, str]):
    if not _logfire_on():
        yield None
        return
    with logfire.span(name, op="mcp.tool", **tags) as span:
        yield span


@contextlib.contextmanager
def tracing_span(name: str, **tags: str):
    with _sentry_span(name, tags), _logfire_span(name, tags):
        yield


# -- metrics -----------------------------------------------------------------
#
# Flat names, djhtmx-style: "damf.<tool>.<outcome>" counters and
# "damf.<tool>.duration_ms" distributions. Sentry >= 2.62 exposes the Trace
# Metrics API as sentry_sdk.metrics.count / .distribution (the old DDM incr
# API was removed in 2.41). Logfire instruments must be created once and
# reused, hence the caches.

_logfire_counters: dict[str, Any] = {}
_logfire_histograms: dict[str, Any] = {}


def metric_incr(name: str, value: int = 1) -> None:
    if _sentry_on() and (count := getattr(sentry_sdk.metrics, "count", None)):
        count(name, value)
    if _logfire_on() and (make_counter := getattr(logfire, "metric_counter", None)):
        instrument = _logfire_counters.get(name)
        if instrument is None:
            instrument = _logfire_counters[name] = make_counter(name)
        instrument.add(value)


def metric_distribution(name: str, value: float) -> None:
    if _sentry_on() and (distribution := getattr(sentry_sdk.metrics, "distribution", None)):
        distribution(name, value)
    if _logfire_on() and (make_histogram := getattr(logfire, "metric_histogram", None)):
        instrument = _logfire_histograms.get(name)
        if instrument is None:
            instrument = _logfire_histograms[name] = make_histogram(name)
        instrument.record(value)


# -- the per-call wrapper ------------------------------------------------------


def _caller_identity() -> tuple[str, str, str]:
    """(user id, username, client name) of the current call, without denying.

    Runs before the tool's own auth so it must never raise: an unauthenticated
    call still deserves a span and a log line, with outcome "denied".
    """
    from django_admin_fastmcp import auth

    try:
        user = auth.current_user()
        return str(user.pk), user.get_username(), auth.client_name()
    except Exception:
        return "", "anonymous", auth.client_name()


@contextlib.contextmanager
def _sentry_user_scope(user_id: str, username: str, tags: dict[str, str]):
    if not _sentry_on():
        yield
        return
    with sentry_sdk.new_scope() as scope:
        if user_id:
            scope.set_user({"id": user_id, "username": username})
        for key, value in tags.items():
            scope.set_tag(key, value)
        yield


def instrument(tool_name: str, fn: Callable) -> Callable:
    """Wrap one tool function with spans, metrics, and the audit log record."""
    wire_name = f"admin_{tool_name}"

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user_id, username, client = _caller_identity()
        model = str(kwargs.get("model", ""))
        tags = {"mcp.tool": wire_name, "mcp.client": client}
        if model:
            tags["mcp.model"] = model

        outcome = "ok"
        started = time.perf_counter()
        try:
            with (
                _sentry_user_scope(user_id, username, tags),
                tracing_span(wire_name, **tags),
            ):
                try:
                    return fn(*args, **kwargs)
                except ToolError:
                    outcome = "denied"
                    raise
                except Exception as error:
                    outcome = "error"
                    if _sentry_on():
                        sentry_sdk.capture_exception(error)
                    raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            metric_incr(f"damf.{tool_name}.{outcome}")
            metric_distribution(f"damf.{tool_name}.duration_ms", duration_ms)
            logger.log(
                logging.INFO if outcome == "ok" else logging.WARNING,
                "tool=%s model=%s user=%s client=%s outcome=%s duration_ms=%.1f",
                wire_name,
                model or "-",
                username,
                client,
                outcome,
                duration_ms,
                extra={
                    "mcp_tool": wire_name,
                    "mcp_model": model,
                    "mcp_user_id": user_id,
                    "mcp_user": username,
                    "mcp_client": client,
                    "mcp_outcome": outcome,
                    "mcp_duration_ms": duration_ms,
                },
            )

    return wrapper
