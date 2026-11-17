"""admin_run_code: one tool call runs a Python snippet that calls the read tools (CODEMODE.md).

The snippet runs in a pydantic-monty sandbox, a Python interpreter in a subprocess, with no
imports beyond its own subset of the standard library, no file system, no network and no
environment.  Each read tool is an async function in the sandbox.  A call goes back to the
host, to the same tool callable FastMCP registered, so the permission checks, the spans and
the audit log record apply unchanged.  Only the snippet's final value goes back to the agent.

Each run gets its own pool of one Monty worker, so nothing survives from one run to the next.
Starting it costs about 10 ms.

Never pass `mount`, `os` or `external_modules` to Monty: each of them opens the host to the
sandbox (a mounted directory makes its `.py` files importable).

"""

import ast
import hashlib
import inspect
import json
import threading
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import anyio
from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from django_admin_fastmcp import conf, tracing
from django_admin_fastmcp.errors import ToolError, ToolRefusalError
from django_admin_fastmcp.tools.declared import ATTRIBUTE, ToolMeta

try:
    import pydantic_monty
except ImportError:  # pragma: no cover - exercised when the extra is absent
    pydantic_monty = None  # type: ignore[assignment]

# The generic tools a snippet may call: the ones that only read.  Writes stay direct calls,
# because their preview protocol needs an agent that reads the preview before it confirms.
READ_TOOLS = (
    "list_models",
    "describe_model",
    "search_objects",
    "get_object",
    "object_history",
    "recent_actions",
    "autocomplete",
)

REDACTED = "[redacted]"

INSTRUCTIONS = (
    " To combine the results of many tool calls in a deterministic way (loop over search "
    "results, join, filter, count), write a Python snippet and send it in one admin_run_code "
    "call instead of calling each tool directly. Only the snippet's final value comes back."
)


def is_available() -> bool:
    """Whether the `codemode` extra (pydantic-monty) is installed."""
    return pydantic_monty is not None


def is_enabled() -> bool:
    """Whether to register admin_run_code: the extra is installed and CODEMODE_ENABLED is on."""
    return is_available() and bool(conf.get("CODEMODE_ENABLED"))


# -- the functions of the sandbox ---------------------------------------------------------


@dataclass(frozen=True)
class SandboxTool:
    """One tool as the sandbox sees it: its registered callable and its argument types."""

    name: str
    fn: Callable
    signature: inspect.Signature
    hints: dict[str, Any]
    adapters: dict[str, TypeAdapter]
    summary: str

    def call(self, args: tuple, kwargs: dict[str, Any]) -> Any:
        """Validate the arguments the way a direct call does, then run the tool.

        Monty hands over raw values, and FastMCP's input validation does not run on this
        path, so the arguments go through pydantic here: a refused argument is a
        `ToolRefusalError`, as it would be on a direct call.

        """
        try:
            bound = self.signature.bind(*args, **kwargs)
        except TypeError as error:
            raise ToolRefusalError(f"{self.name}: {error}") from None
        try:
            arguments = {
                name: self.adapters[name].validate_python(value) if name in self.adapters else value
                for name, value in bound.arguments.items()
            }
        except ValidationError as error:
            raise ToolRefusalError(f"{self.name}: invalid arguments: {error}") from None
        return self.fn(**arguments)


def select_sandbox_tools(registered: Mapping[str, Callable]) -> dict[str, SandboxTool]:
    """The read tools among `registered`, the tools of the server keyed by catalogue name.

    The generic read tools of `READ_TOOLS`, and the declared tools without `write=True`.
    A tool that `DISABLED_TOOLS` removed from the server is absent from `registered`, so
    it is absent from the sandbox too.

    """
    return {
        name: _describe_tool(name, fn)
        for name, fn in registered.items()
        if name in READ_TOOLS or _is_declared_read(fn)
    }


def _is_declared_read(fn: Callable) -> bool:
    meta = getattr(fn, ATTRIBUTE, None)
    return isinstance(meta, ToolMeta) and not meta.write


def _describe_tool(name: str, fn: Callable) -> SandboxTool:
    # The registered callable is wrapped (tracing, connections); the signature and the
    # hints are those of the tool function underneath.
    tool_function = inspect.unwrap(fn)
    signature = inspect.signature(fn)
    hints = typing.get_type_hints(tool_function)
    adapters = {
        parameter: TypeAdapter(hint) for parameter, hint in hints.items() if parameter != "return"
    }
    summary = (inspect.getdoc(tool_function) or "").strip().split("\n")[0]
    return SandboxTool(
        name=name, fn=fn, signature=signature, hints=hints, adapters=adapters, summary=summary
    )


# -- the stubs the agent reads ------------------------------------------------------------


def render_stubs(tools: Mapping[str, SandboxTool]) -> str:
    """Python stubs of the sandbox functions: the agent reads them, and Monty type-checks
    the snippet against them before it runs it.

    A type the stubs cannot name in the sandbox (a model class, a UUID) becomes `Any`; the
    host still validates the value when the call arrives.

    """
    imports: set[str] = set()
    functions = [_render_stub(tool, imports) for tool in tools.values()]
    module_imports = [f"import {module}" for module in sorted(imports)]
    header = ["from typing import Any, Literal", *module_imports]
    return "\n".join(header) + "\n\n" + "\n\n".join(functions) + "\n"


def _render_stub(tool: SandboxTool, imports: set[str]) -> str:
    parameters = [
        _render_parameter(parameter, tool.hints.get(name, Any), imports)
        for name, parameter in tool.signature.parameters.items()
        if parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
    ]
    # A declared tool has no return hint: it always answers with a dict, whatever its
    # method returns.
    returned = (
        _render_type(tool.hints["return"], imports) if "return" in tool.hints else "dict[str, Any]"
    )
    docstring = f'\n    """{tool.summary}"""' if tool.summary else " ..."
    return f"async def {tool.name}({', '.join(parameters)}) -> {returned}:{docstring}"


def _render_parameter(parameter: inspect.Parameter, hint: Any, imports: set[str]) -> str:
    rendered = f"{parameter.name}: {_render_type(hint, imports)}"
    if parameter.default is inspect.Parameter.empty:
        return rendered
    elif isinstance(parameter.default, (type(None), bool, int, float, str)):
        return f"{rendered} = {parameter.default!r}"
    else:
        return f"{rendered} = ..."


def _render_type(hint: Any, imports: set[str]) -> str:
    origin = typing.get_origin(hint)
    arguments = typing.get_args(hint)
    if hint is None or hint is type(None):
        return "None"
    elif hint is Any:
        return "Any"
    elif origin in (typing.Union, types.UnionType):
        return " | ".join(_render_type(argument, imports) for argument in arguments)
    elif origin is Literal:
        return f"Literal[{', '.join(repr(argument) for argument in arguments)}]"
    elif origin in (list, dict, set, tuple):
        return f"{origin.__name__}[{', '.join(_render_type(a, imports) for a in arguments)}]"
    elif isinstance(hint, type) and hint.__module__ == "builtins":
        return hint.__name__
    elif isinstance(hint, type) and hint.__module__ == "datetime":
        imports.add("datetime")
        return f"datetime.{hint.__name__}"
    else:
        return "Any"


def describe_run_code(stubs: str) -> str:
    """The description of admin_run_code: when to use it, the rules, the limits, the stubs."""
    return f"""\
Run a Python snippet that calls the read tools below and combines their results; only the \
snippet's final value comes back.

Use it instead of many direct calls when the results of several tool calls must be combined \
in a deterministic way: loop over search results, join, filter, count.

Rules:
- The snippet is Python, run by a sandboxed interpreter.  Each function below is async: call \
it with `await`, by keyword, and run calls in parallel with `asyncio.gather`.
- The value of the last expression is the result.  `print` output comes back as `stdout`.
- Only part of the standard library imports (asyncio, json, re, datetime, math, itertools, \
collections, ...).  There are no other modules, no files, no network and no environment.
- The snippet is type-checked against the functions below before it runs.
- A refused call raises an exception, which the snippet may catch.
- Each call reads the database at its own moment, not from one snapshot.
- Limits per run: {conf.get("CODEMODE_TIMEOUT")} seconds, {conf.get("CODEMODE_MAX_CALLS")} \
calls, {conf.get("CODEMODE_MAX_PARALLEL")} calls at the same time, and \
{conf.get("CODEMODE_MAX_RESULT_BYTES")} bytes of result and stdout.  A larger result is \
refused: reduce it in the snippet.

Functions:

```python
{stubs}```
"""


# -- the snippet in the span and the audit record -----------------------------------------


class _LiteralRedactor(ast.NodeTransformer):
    """Replace string and bytes literals with REDACTED, except the ones that name the
    schema: dict keys (field names and lookups) and the value of `model=`."""

    def visit_Dict(self, node: ast.Dict) -> ast.Dict:
        node.values = [self.visit(value) for value in node.values]
        return node

    def visit_keyword(self, node: ast.keyword) -> ast.keyword:
        if node.arg == "model":
            return node
        else:
            return self.generic_visit(node)  # type: ignore[return-value]

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if isinstance(node.value, (str, bytes)):
            return ast.copy_location(ast.Constant(REDACTED), node)
        else:
            return node


def redact_snippet(code: str) -> str | None:
    """The snippet without its data, for the span and the audit record; None when it does
    not parse.

    String and bytes literals become "[redacted]", except dict keys and the value of
    `model=`; numbers, booleans and None stay; comments go, because the result is
    `ast.unparse` of the redacted tree.

    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    return ast.unparse(_LiteralRedactor().visit(tree))


# -- the run ------------------------------------------------------------------------------


def build_run_code(registered: Mapping[str, Callable]) -> tuple[Callable, str]:
    """The admin_run_code tool function and its description, over the read tools among
    `registered` (the server's tools, keyed by catalogue name)."""
    tools = select_sandbox_tools(registered)
    stubs = render_stubs(tools)

    async def run_code(code: str) -> dict[str, Any]:
        """Run a Python snippet that calls the read tools; see the tool description."""
        return await _run_snippet(code, tools, stubs)

    return run_code, describe_run_code(stubs)


def _invoke_in_thread(tool: SandboxTool, args: tuple, kwargs: dict, finished: threading.Event):
    # A call can wait in the limiter's queue past the end of the run: Monty does not cancel
    # the host calls it abandons.  The check runs in the worker thread, right before the
    # tool, so no query starts once the run is over.
    if finished.is_set():
        raise ToolRefusalError("the run is over")
    try:
        return tool.call(args, kwargs)
    except ToolError:
        raise
    except Exception:
        # instrument() already logged the bug and sent it to Sentry; the snippet, and the
        # agent behind it, get no internals, as with a direct call.
        raise ToolError(f"{tool.name} failed with an internal error") from None


async def _run_snippet(code: str, tools: Mapping[str, SandboxTool], stubs: str) -> dict[str, Any]:
    assert pydantic_monty is not None
    timeout = conf.get("CODEMODE_TIMEOUT")
    max_calls = conf.get("CODEMODE_MAX_CALLS")
    max_bytes = conf.get("CODEMODE_MAX_RESULT_BYTES")
    tracing.annotate(
        snippet=redact_snippet(code),
        snippet_sha256=hashlib.sha256(code.encode()).hexdigest(),
    )

    limiter = anyio.CapacityLimiter(conf.get("CODEMODE_MAX_PARALLEL"))
    finished = threading.Event()
    calls = 0
    printed: list[str] = []
    printed_bytes = 0

    def bridge(tool: SandboxTool) -> Callable:
        async def call(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > max_calls:
                raise ToolRefusalError(f"over CODEMODE_MAX_CALLS={max_calls}")
            return await anyio.to_thread.run_sync(
                _invoke_in_thread, tool, args, kwargs, finished, limiter=limiter
            )

        return call

    def collect_print(_stream: str, text: str) -> None:
        nonlocal printed_bytes
        printed_bytes += len(text.encode())
        if printed_bytes <= max_bytes:
            printed.append(text)

    limits = {
        # Monty's clock stops while the snippet waits on the host, so it bounds computation
        # only; fail_after bounds the whole run, tool calls included.
        "max_feed_duration_secs": timeout,
        "max_memory": conf.get("CODEMODE_MAX_MEMORY_BYTES"),
        # Monty counts about one suspension per call; CODEMODE_MAX_CALLS is the real cap.
        "max_suspensions": 2 * max_calls + 100,
    }
    try:
        with anyio.fail_after(timeout):
            async with (
                pydantic_monty.AsyncMonty(max_processes=1) as pool,
                pool.checkout(
                    script_name="snippet.py",
                    limits=limits,
                    type_check=True,
                    type_check_stubs=stubs,
                ) as session,
            ):
                output = await session.feed_run(
                    code,
                    external_lookup={name: bridge(tool) for name, tool in tools.items()},
                    print_callback=collect_print,
                )
    except (pydantic_monty.MontySyntaxError, pydantic_monty.MontyTypingError) as error:
        raise ToolRefusalError(f"the snippet does not type-check:\n{error}") from None
    except pydantic_monty.MontyRuntimeError as error:
        raise ToolRefusalError(f"the snippet failed:\n{error.display('traceback')}") from None
    except TimeoutError:
        raise ToolRefusalError(f"the run took over CODEMODE_TIMEOUT={timeout} seconds") from None
    finally:
        finished.set()
        tracing.annotate(calls=calls)

    if calls > max_calls:
        # The snippet may have caught the refusal and carried on with partial data.
        raise ToolRefusalError(
            f"the snippet made {calls} calls, over CODEMODE_MAX_CALLS={max_calls}"
        )
    payload = {"result": to_jsonable_python(output, fallback=repr), "stdout": "".join(printed)}
    size = max(len(json.dumps(payload).encode()), printed_bytes)
    if size > max_bytes:
        raise ToolRefusalError(
            f"the result is {size} bytes, over CODEMODE_MAX_RESULT_BYTES={max_bytes}: "
            "reduce it in the snippet"
        )
    return payload
