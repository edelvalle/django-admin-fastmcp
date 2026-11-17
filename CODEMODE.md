# Code mode

> **Draft.**  Code mode is not released yet.  This document describes the intended behavior, and [the design plan](docs/plans/codemode.md) records its decisions.  Points marked **Open** follow decisions that the plan has not made yet.

Code mode lets an agent send a short Python snippet in one tool call.  The snippet calls several admin tools and combines their output, and only its final value goes back to the agent.

This has two benefits:

- **Fewer round trips.**  A task that needs a search and then one call for each result (1 + N calls) becomes a single call.
- **Less context.**  The intermediate results stay on the server.  The agent receives only the value that the snippet computes from them.

The snippet runs in a [pydantic-monty](https://github.com/pydantic/monty) sandbox.  Inside the sandbox each admin tool is an async function, and the snippet can't reach anything else: no Django imports, no file system, no network.

## Installation

Code mode is an optional extra:

```bash
uv add "django-admin-fastmcp[codemode]"
```

Then turn it on with `CODEMODE_ENABLED` (see [Settings](#settings)).  You write no code:

```python
ADMIN_FASTMCP = {
    ...,
    "CODEMODE_ENABLED": True,
}
```
  When the extra is installed and the setting turns it on, the server registers one more tool, `admin_run_code`, at the same `MCP_URL` as the other tools.  `DISABLED_TOOLS` can remove it like any other tool (`"run_code"`).

A startup check reports an error when `CODEMODE_ENABLED` turns the tool on but the extra is not installed.

## How an agent uses it

`admin_run_code` takes one argument, `code`.  Its description tells the agent the rules of the sandbox and lists a stub for each function the snippet can call.  The server generates these stubs from the tools themselves, so they include the [declared tools](README.md#declared-tools) of your `ModelAdmin` classes:

```python
async def search_objects(model: str, q: str | None = None, filters: dict[str, Any] | None = None,
                         order_by: list[str] | None = None, page: int = 1,
                         page_size: int = 25) -> dict[str, Any]: ...
async def get_object(model: str, pk: str) -> dict[str, Any]: ...
async def object_history(model: str, pk: str) -> list[dict[str, Any]]: ...
```

For example, this snippet finds the open orders that have no admin history.  It is one tool call instead of 51:

```python
orders = await search_objects(model="demo.order", filters={"status": "open"}, page_size=50)
histories = await asyncio.gather(*(
    object_history(model="demo.order", pk=o["pk"]) for o in orders["results"]
))
[
    {"order": o["pk"], "admin_url": o["admin_url"]}
    for o, h in zip(orders["results"], histories)
    if not h
]
```

The result has two keys: `result`, the value of the last expression, and `stdout`, the captured `print` output.

The server type-checks the snippet against the stubs before it runs it.  A misspelled function or a wrong argument type fails before the first tool call.

## Which tools a snippet can call

**Open.**  The read tools are always available: `list_models`, `describe_model`, `search_objects`, `get_object`, `object_history`, `recent_actions`, `autocomplete`, and the declared tools without `write=True`.

Write tools are not decided yet.  The preview protocol of `delete_object` and `run_action` assumes that the agent reads the preview before it confirms, and inside a snippet nobody reads it.  The options in the plan range from read tools only to every tool, with confirmation forced off or turned into proposals that the agent applies with direct calls.

## Permissions

A call from a snippet runs the same tool code as a direct call, as the same user:

- The `ModelAdmin` permission checks, `WRITABLE_MODELS` and `REDACT_FIELDS` apply unchanged.  Code mode adds no permission system of its own.
- Arguments are validated and coerced the same way as in a direct call.
- A refusal (`ToolRefusalError`) reaches the snippet as an exception, and the snippet can catch it.  An exception that the snippet doesn't catch stops the run, and the tool result shows the message and the line of the snippet.

## Limits

Each run is bounded:

- A timeout for the whole run.
- A maximum number of tool calls.
- A maximum number of tool calls running at the same time.  Each one holds a database connection, so the default suits a small connection pool.
- A maximum size for the result.  **Open:** the server either refuses a result that is too large, telling the agent to reduce it in the snippet, or truncates it with a marker.
- The memory, allocation and recursion limits of the Monty sandbox.

Each tool call inside a snippet is its own unit of work, as with direct calls.  **Open:** whether a whole run reads one consistent snapshot in a single read-only transaction.

## Settings

The keys live in the `ADMIN_FASTMCP` dict, next to the other settings, with the prefix `CODEMODE_`.

**Open.**  The keys below are the planned ones.  Their defaults, and whether code mode is on by default once the extra is installed, are not decided yet.

| Key | Meaning |
|---|---|
| `CODEMODE_ENABLED` | Registers `admin_run_code`. |
| `CODEMODE_TIMEOUT` | Seconds a whole run may take. |
| `CODEMODE_MAX_CALLS` | Tool calls a run may make. |
| `CODEMODE_MAX_PARALLEL` | Tool calls that may run at the same time.  Raise it with your database connection pool. |
| `CODEMODE_MAX_RESULT_BYTES` | Maximum size of the result. |

## Observability

A run opens one span, `admin_run_code`, and the spans of the tool calls it makes nest under it.  The audit log keeps one record for each tool call, as for direct calls, and one record for the run with the number of calls.

**Open:** whether the span and the audit record keep the snippet itself (with the usual redaction) or only a hash of it.  A snippet can contain search terms and personal data.
