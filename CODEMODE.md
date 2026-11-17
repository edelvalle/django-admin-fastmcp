# Code mode

Code mode lets an agent send a short Python snippet in one tool call.  The snippet calls several read tools and combines their output, and only its final value goes back to the agent.

This has two benefits:

- **Fewer round trips.**  A task that needs a search and then one call for each result (1 + N calls) becomes a single call.
- **Less context.**  The intermediate results stay on the server.  The agent receives only the value that the snippet computes from them.

The snippet runs in a [pydantic-monty](https://github.com/pydantic/monty) sandbox: a Python interpreter in a separate process.  Inside the sandbox each read tool is an async function, and the snippet can't reach anything else.  It can import only Monty's own part of the standard library (no Django, no project code, no installed packages), and it has no file system, no network and no environment variables.

[The design plan](docs/plans/codemode.md) records the decisions behind this document.

## Installation

Code mode is an optional extra:

```bash
uv add "django-admin-fastmcp[codemode]"
```

It is off by default.  Turn it on with `CODEMODE_ENABLED`; you write no code:

```python
ADMIN_FASTMCP = {
    ...,
    "CODEMODE_ENABLED": True,
}
```

The server then registers one more tool, `admin_run_code`, at the same `MCP_URL` as the other tools, with the MCP hint `readOnlyHint`.  `DISABLED_TOOLS` can remove it like any other tool (`"run_code"`).  A startup check (`django_admin_fastmcp.E008`) reports an error when `CODEMODE_ENABLED` is on but the extra is not installed.

## How an agent finds and uses it

With code mode on, the server instructions, which a client receives when it connects, gain a paragraph between the orientation and the preview rule of the destructive tools:

> Prefer admin_run_code for every read that takes more than one tool call: a search followed by a call per result, combining several models, filtering, grouping or counting rows, or checking many objects. Write one Python snippet that awaits the read tools and computes the answer, and send it in a single admin_run_code call. It is faster than calling the tools one by one, the computation is deterministic, and the intermediate results stay out of your context: only the snippet's final value comes back. Call the read tools directly only for a single lookup. admin_run_code is read-only: a snippet can call only the read tools, so create, update, delete and run actions with the write tools, one direct call each.

An agent left to itself keeps calling the direct tools it already knows, so the paragraph is a rule with its reasons, not a mention.

`admin_run_code` takes one argument, `code`.  Its description says when to use it, gives the rules and the limits of the sandbox, and lists a stub for each function the snippet can call.  The server generates these stubs from the tools themselves, so they include the read-only [declared tools](README.md#declared-tools) of your `ModelAdmin` classes:

```python
async def search_objects(model: str, q: str | None = None, filters: dict[str, Any] | None = None, order_by: list[str] | None = None, page: int = 1, page_size: int = 25) -> dict[str, Any]:
    """Search rows of one model, as the admin changelist would."""

async def object_history(model: str, pk: str) -> list[dict[str, Any]]:
    """Read the admin history of one object, newest first."""
```

For example, this snippet finds the open orders that have no admin history.  It is one tool call instead of 51:

```python
import asyncio

orders = await search_objects(model="shop.order", filters={"status": "open"}, page_size=50)
histories = await asyncio.gather(
    *(object_history(model="shop.order", pk=row["pk"]) for row in orders["rows"])
)
[
    {"order": row["pk"], "admin_url": row["admin_url"]}
    for row, history in zip(orders["rows"], histories)
    if not history
]
```

The tool result has two keys: `result`, the value of the last expression, and `stdout`, the captured `print` output.

The server type-checks the snippet against the stubs before it runs it.  A misspelled function, a write tool, or a wrong argument type fails before the first tool call, and the error points at the line of the snippet.

## Which tools a snippet can call

Only the tools that read: `list_models`, `describe_model`, `search_objects`, `get_object`, `object_history`, `recent_actions`, `autocomplete`, and the declared tools without `write=True`.  A tool that `DISABLED_TOOLS` removes is absent from the sandbox too.

The write tools stay direct calls.  The preview protocol of `delete_object` and `run_action` relies on an agent that reads the preview before it confirms, and inside a snippet nobody reads it.

## Permissions

A call from a snippet runs the same tool code as a direct call, as the same user:

- The `ModelAdmin` permission checks and `REDACT_FIELDS` apply unchanged.  Code mode adds no permission system of its own.
- Arguments are validated and coerced the same way as in a direct call.
- A refused call raises an exception in the snippet, which the snippet may catch.  An exception that the snippet doesn't catch stops the run, and the tool result shows the message and a traceback with the line of the snippet.
- A bug in a tool reaches the snippet as an internal error, without details.  The details go to the log and to Sentry, as for a direct call.

Each tool call is its own unit of work, as with direct calls: the calls of one run read the database at different moments, not from one snapshot.

## Limits

| Key | Default | Meaning |
|---|---|---|
| `CODEMODE_ENABLED` | `False` | Registers `admin_run_code`. |
| `CODEMODE_TIMEOUT` | `30` | Seconds a whole run may take, tool calls included. |
| `CODEMODE_MAX_CALLS` | `100` | Tool calls a run may make.  Over it, the run is refused, even when the snippet catches the error. |
| `CODEMODE_MAX_PARALLEL` | `4` | Tool calls that may run at the same time.  Each one holds a database connection: raise it with your connection pool. |
| `CODEMODE_MAX_RESULT_BYTES` | `65536` | Maximum size of the result and of the `print` output, as JSON.  A larger result is refused, and the agent is told to reduce it in the snippet. |
| `CODEMODE_MAX_MEMORY_BYTES` | `67108864` | Heap memory of the sandbox (64 MiB). |

When a run times out, it stops at once and no new tool call starts.  A tool call that is already running a query can't be stopped, so that query finishes in the background and its result is dropped.

Each run starts its own Monty worker process, which costs about 10 ms, and the worker ends with the run: nothing survives from one run to the next.

## Observability

A run opens one span, `admin_run_code`, and the spans of the tool calls it makes nest under it.  The audit log keeps one record for each tool call, as for direct calls, and one record for the run, with the number of calls (`mcp_calls`).

The span and the audit record of the run keep the snippet with its data redacted (`mcp_snippet`), plus a SHA-256 hash of the original snippet to group the runs of the same one (`mcp_snippet_sha256`).  A snippet can contain search terms and personal data, so before it records the snippet the server:

- replaces every string literal with `"[redacted]"`, except dict keys (field names and lookups such as `"status__in"`) and the value of `model=`;
- drops the comments.

Numbers, booleans and `None` stay.  A snippet that is not valid Python is recorded by its hash only.

For example, this snippet:

```python
# customers that wrote from Maria's address
await search_objects(model="crm.customer", q="maria.garcia@example.com", filters={"status": "active"})
```

is recorded as:

```python
await search_objects(model='crm.customer', q='[redacted]', filters={'status': '[redacted]'})
```
