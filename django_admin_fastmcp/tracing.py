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
- the exception, for outcomes that are bugs rather than denials.  A global
  event processor drops the error events of a `ToolRefusalError`, which
  Sentry's own MCP integration would report.
"""

import contextlib
import contextvars
import functools
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any

import anyio

from django_admin_fastmcp import conf
from django_admin_fastmcp.errors import ToolError, ToolRefusalError

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
    # Logfire records the message and traceback of an exception that leaves the span, and
    # tool errors quote the caller's data (a pk, a filter value, a snippet's traceback).  The
    # exception leaves the span already caught, so the span keeps its type only.
    error: BaseException | None = None
    with logfire.span(name, op="mcp.tool", **tags) as span:
        try:
            yield span
        except BaseException as exc:
            error = exc
            span.set_level("warn" if isinstance(exc, ToolError) else "error")
            span.set_attribute("mcp.error_type", type(exc).__qualname__)
    if error is not None:
        raise error


@contextlib.contextmanager
def tracing_span(name: str, **tags: str):
    """Open the Sentry and Logfire spans of `name`, when their backends are on.

    Yields a function `set_attribute(key, value)` that sets an attribute on every open span,
    and does nothing when no backend is on.

    """
    with _sentry_span(name, tags) as sentry_span, _logfire_span(name, tags) as logfire_span:

        def set_attribute(key: str, value: Any) -> None:
            if sentry_span is not None:
                sentry_span.set_data(key, value)
            if logfire_span is not None:
                logfire_span.set_attribute(key, value)

        yield set_attribute


# -- Sentry events -------------------------------------------------------------


def drop_refusal_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Sentry event processor: drop the error event of a `ToolRefusalError`, keep the rest.

    A refusal is the caller's mistake, not a server fault, yet Sentry's own MCP integration
    (enabled by itself when `mcp` is installed) reports every exception a tool raises.

    """
    exc_info = hint.get("exc_info")
    if exc_info is not None and isinstance(exc_info[1], ToolRefusalError):
        return None
    else:
        return event


def install_sentry_event_processor() -> None:
    """Register `drop_refusal_event`:func: as a global Sentry event processor, once.

    Does nothing without `sentry_sdk`.  Global processors run on every event, whichever
    client sends it, and alongside the processors of other libraries.

    """
    if sentry_sdk is not None:
        from sentry_sdk.scope import add_global_event_processor, global_event_processors

        if drop_refusal_event not in global_event_processors:
            add_global_event_processor(drop_refusal_event)


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


# The fields `annotate()` adds to the current tool call.  A context variable, so that a tool
# calling other tools (admin_run_code) annotates its own call: each inner call sets its own
# dict, in the copy of the context its worker thread runs in.
_fields: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "django_admin_fastmcp_fields", default=None
)


def annotate(**fields: Any) -> None:
    """Add `fields` to the span and the audit log record of the current tool call.

    Each field becomes the span attribute `mcp.<name>` and the log record attribute
    `mcp_<name>`.  A later call with the same name replaces the value.  Outside an
    instrumented tool call it does nothing.

    """
    current = _fields.get()
    if current is not None:
        current.update(fields)


@contextlib.contextmanager
def _instrumented_call(wire_name: str, tool_name: str, model_label: str, identity):
    user_id, username, client = identity
    tags = {"mcp.tool": wire_name, "mcp.client": client}
    if model_label:
        tags["mcp.model"] = model_label

    fields: dict[str, Any] = {}
    fields_token = _fields.set(fields)
    outcome = "ok"
    started = time.perf_counter()
    try:
        with (
            _sentry_user_scope(user_id, username, tags),
            tracing_span(wire_name, **tags) as set_attribute,
        ):
            try:
                yield
            except ToolError:
                outcome = "denied"
                raise
            except Exception as error:
                outcome = "error"
                if _sentry_on():
                    sentry_sdk.capture_exception(error)
                raise
            finally:
                for key, value in fields.items():
                    set_attribute(f"mcp.{key}", value)
    finally:
        _fields.reset(fields_token)
        duration_ms = (time.perf_counter() - started) * 1000
        metric_incr(f"damf.{tool_name}.{outcome}")
        metric_distribution(f"damf.{tool_name}.duration_ms", duration_ms)
        logger.log(
            logging.INFO if outcome == "ok" else logging.WARNING,
            "tool=%s model=%s user=%s client=%s outcome=%s duration_ms=%.1f",
            wire_name,
            model_label or "-",
            username,
            client,
            outcome,
            duration_ms,
            extra={
                "mcp_tool": wire_name,
                "mcp_model": model_label,
                "mcp_user_id": user_id,
                "mcp_user": username,
                "mcp_client": client,
                "mcp_outcome": outcome,
                "mcp_duration_ms": duration_ms,
                **{f"mcp_{key}": value for key, value in fields.items()},
            },
        )


def _caller_identity_in_thread() -> tuple[str, str, str]:
    from django_admin_fastmcp.auth import with_fresh_connections

    return with_fresh_connections(_caller_identity)()


def instrument(tool_name: str, fn: Callable, *, model: str | None = None) -> Callable:
    """Wrap one tool function with spans, metrics, and the audit log record.

    A generic tool names its model in the `model` argument of each call; a declared tool
    acts on one model, given here.  `fn` may be sync or async; an async `fn` gets an
    async wrapper, which looks up the caller in a worker thread, because the ORM is not
    allowed on the event loop.

    """
    wire_name = f"admin_{tool_name}"

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            model_label = model if model is not None else str(kwargs.get("model", ""))
            identity = await anyio.to_thread.run_sync(_caller_identity_in_thread)
            with _instrumented_call(wire_name, tool_name, model_label, identity):
                return await fn(*args, **kwargs)

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        model_label = model if model is not None else str(kwargs.get("model", ""))
        with _instrumented_call(wire_name, tool_name, model_label, _caller_identity()):
            return fn(*args, **kwargs)

    return wrapper
