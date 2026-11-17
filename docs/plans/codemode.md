# Code mode: one tool call runs a snippet that calls many tools

## Goal

An agent (for example Claude Code) sends a short Python snippet in one tool call.  The snippet calls several admin tools and combines their output.  Only the final value goes back to the agent.

This gives two gains:

- Fewer round trips.  A task that needs 1 + N tool calls (a search, then one call for each result) becomes one call.
- Less context.  The intermediate results stay on the server.  The agent receives only the value that the snippet computes from them.

The snippet runs in a [pydantic-monty](https://github.com/pydantic/monty) sandbox, in the async path of the ASGI app.  Inside the sandbox, each tool is an async function.

The package provides code mode on its own: an optional extra and the `CODEMODE_*` keys of `ADMIN_FASTMCP`.  A consumer installs the extra and turns the setting on.  The consumer writes no code.

This document describes the design and its decisions.  All decisions are made, and code mode is implemented in `django_admin_fastmcp/tools/codemode.py`.

## Current state

- `server.build_server()` registers the generic tools of `all_tools()` and the declared tools of `declared()`, under the namespace `admin` (SPEC.md section 4).
- All tools are sync `def`.  FastMCP runs each one in a worker thread, where the sync ORM is legal (SPEC.md section 6, decision D6).
- Each tool goes through two wrappers: `tracing.instrument(name, fn)` (spans, metrics, the audit log record) and `auth.with_fresh_connections` (`close_old_connections` before and after).
- A tool finds the user with `auth.current_user()`.  It reads the access token through `fastmcp.server.dependencies.get_access_token()`, which uses a context variable.
- `tools.resolve()` applies the permission checks in each tool: staff user, exposed model, registered `ModelAdmin`, `has_<action>_permission`.  Writes also pass `require_write()` (`WRITABLE_MODELS`).
- `delete_object` and `run_action` preview by default.  They change nothing until the agent calls again with `confirm=True` (SPEC.md section 9, rail 4).
- `conf.get()` raises `KeyError` on an unknown key, and `checks.py` validates `ADMIN_FASTMCP` at startup.
- `admin_mcp_serve` serves the app with `stateless_http=True`, behind `BodySizeLimit` (256 KiB request body).
- The token verifier compares the token audience with `MCP_URL` in full.  FastMCP advertises that URL as the protected resource.
- The optional extras today are `sentry` and `logfire`.  Each backend activates only when its library is importable and its flag is on.

## Proposed shape

A new tool, `admin_run_code(code: str)`, is an `async def` tool.  It compiles the snippet with Monty and runs it on the event loop.  Each function that the snippet calls is a bridge back to an existing tool callable, with the same wrappers and the same permission checks.  The sandbox has no other access to the outside world: no imports of Django, no file system, no network.

```
MCP client ──▶ MCP_URL (admin_mcp_serve, or a mounted app)
                 ├─ admin_search_objects   (sync, worker thread)   ← unchanged
                 ├─ admin_get_object …
                 └─ admin_run_code         (async, event loop)
                        └─ Monty snippet
                             await search_objects(...)  ─┐
                             await get_object(...)      ─┤ external functions
                                                         ▼
                             bridge → worker thread → instrument(with_fresh_connections(tool))
```

### Packaging and settings

- A new extra, `codemode = ["pydantic-monty>=…"]`, next to `sentry` and `logfire`.
- New flat keys in `conf.DEFAULTS`, with the prefix `CODEMODE_` (decision 6).  Flat keys keep their defaults one by one, because `conf.get()` replaces a value as a whole, and the startup check of unknown keys covers them without change.
- `build_server()` registers `run_code` only when `pydantic_monty` is importable and `CODEMODE_ENABLED` is on.  This is the same rule as the tracing backends.
- `run_code` is a name in `all_tools()`, so `DISABLED_TOOLS` can remove it and `checks.py` accepts the name.
- `checks.py` reports an error when `CODEMODE_ENABLED` is on but the extra is not installed.

### What the agent sees

The server instructions, which a client receives when it connects and usually shows to the model before it loads any tool, gain one sentence when `CODEMODE_ENABLED` is on.  `build_server()` appends it to the text it passes to `FastMCP(instructions=...)`:

> To combine the results of many tool calls in a deterministic way (loop over search results, join, filter, count), write a Python snippet and send it in one `admin_run_code` call instead of calling each tool directly.  Only the snippet's final value comes back.

Without this sentence the agent has no reason to prefer the tool: the direct tools are in the same catalogue, and clients that load tools on demand show little more than the name and the first line of each description.  For the same reason, the first line of the description says when to use the tool, not only what it is.

The description of `admin_run_code` contains:

- The rules of the sandbox (what Python it supports, how to return a value, the limits).
- Type stubs that the server generates from the signatures and the first docstring line of each tool in the sandbox.  The declared tools of the consumer's `ModelAdmin` classes are included, so the stubs differ for each project.

```python
async def search_objects(model: str, q: str | None = None, filters: dict[str, Any] | None = None,
                         order_by: list[str] | None = None, page: int = 1,
                         page_size: int = 25) -> dict[str, Any]: ...
async def get_object(model: str, pk: str) -> dict[str, Any]: ...
async def object_history(model: str, pk: str) -> list[dict[str, Any]]: ...
```

An example call, with the `demo` app of the test project:

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

The result contains the value of the last expression and the captured `print` output.

### How a run works

The API of pydantic-monty 1.6.0 differs from its README: `AsyncMonty` is a pool of `monty` worker processes, `checkout()` gives a session, and `session.feed_run(code, external_lookup=..., print_callback=...)` runs the snippet.  A run of `admin_run_code`:

1. Records the redacted snippet and its hash (decision 8) through `tracing.annotate()`.
2. Opens a pool of one worker (decision 9), checks out a session with the stubs, `type_check=True` and the Monty limits, and feeds the snippet.  The whole run is inside `anyio.fail_after(CODEMODE_TIMEOUT)` (decision 10).
3. Serves each function of the sandbox with a bridge: it counts the call against `CODEMODE_MAX_CALLS`, then runs the registered tool callable in a worker thread through `anyio.to_thread.run_sync`, under a `CapacityLimiter(CODEMODE_MAX_PARALLEL)`.  In the thread it validates the arguments with pydantic, as FastMCP does for a direct call.
4. Converts the result to JSON and refuses it when it is larger than `CODEMODE_MAX_RESULT_BYTES` (decision 7).

The bridge refuses to start a tool once the run is over, whatever ended it.  Monty does not cancel the host calls that it abandons, so without this check the calls that wait in the limiter's queue run after a timeout.  The check runs in the worker thread, right before the tool, because a queued call reaches a thread only after the limiter lets it through.

Never pass `mount`, `os` or `external_modules` to Monty.  With the defaults a snippet can import only Monty's own part of the standard library, and it has no file system, network or environment: this was verified against 1.6.0, with the code of a Django project on the host's `PYTHONPATH` and in the working directory.  A mounted directory makes its `.py` files importable in the sandbox, and an `os` handler gives the sandbox whatever the handler returns.

## Properties that hold for all options

- **Same permissions.**  A call from the sandbox goes through the same tool callable as a direct call.  `resolve()`, `require_write()` and `REDACT_FIELDS` apply without change.  Design principle 1 (no parallel permission system) holds.
- **Argument validation.**  Monty gives raw values to the external functions, and FastMCP does not validate them.  Each callable in the sandbox needs `pydantic.validate_call` (or the input schema of FastMCP) to coerce and refuse arguments the same way as a direct call.
- **Early failure.**  Monty can type-check the snippet against the stubs before it runs.  A wrong name or a wrong argument type then fails before the first tool call.
- **Errors in the snippet.**  A `ToolRefusalError` from a tool goes into the sandbox as an exception.  The snippet can catch it.  An exception that the snippet does not catch stops the run, and the tool result shows the message and the line of the snippet.
- **Tracing and audit.**  `instrument("run_code", ...)` opens one span for the run, and it holds the spans that `instrument()` opens for the inner calls.  The audit log keeps one record for each inner call, and one record for the run.  The run records the count of calls, and the redacted snippet of decision 8.
- **User identity in the inner calls.**  `current_user()` reads a context variable.  The bridge must carry the context into each inner call.  `anyio.to_thread.run_sync` copies the context into the thread; verify this with the access-token variable of FastMCP before anything else.
- **Decision D6 holds for the tools.**  The admin tools stay sync.  Only `run_code` is async, because it runs no admin code itself.

## Decisions

### 1. Where the code lives — decided: in this package

Code mode is part of `django-admin-fastmcp`, as the `codemode` extra.  It depends on the internals of the package (`all_tools`, `declared`, `instrument`, `with_fresh_connections`).  A consumer that kept it in its own code would depend on these internals.

### 2. Which tools the sandbox exposes — decided: A

The preview protocol of destructive tools depends on the agent: it reads the preview, and then it confirms.  In a snippet, `run_action(..., confirm=True)` runs without a preview that anybody reads.

- **A. Read tools only**: `list_models`, `describe_model`, `search_objects`, `get_object`, `object_history`, `recent_actions`, `autocomplete`, and the declared tools without `write=True`.  `admin_run_code` then can carry `readOnlyHint`.  Writes stay one direct call for each tool.
- **B. Reads, plus writes that only collect proposals.**  In the sandbox, a write tool returns a proposed change and does not apply it.  The tool result lists the proposals.  The agent applies them with direct calls, or with a second tool (for example `admin_apply_proposals`).
- **C. All tools, with `confirm` forced to `false` in the sandbox.**  Snippets can preview many changes, but they cannot apply them.
- **D. All tools, without restriction.**  The preview protocol then depends only on the agent's own behavior.

Decision D8 of SPEC.md (a cap on superuser tokens) is still open, and it affects this decision.

The current decision is A.  `admin_run_code` carries `readOnlyHint`, and a write tool in a snippet fails the type check, because it has no stub.

### 3. Which endpoint serves it — decided: A

- **A. One more tool on the existing app** (`MCP_URL`).  The OAuth grant, the token audience and the client configuration stay the same.  The async tool is the async path.
- **B. A dedicated app or mount** (for example `<MCP_URL>/code`, or a separate process).  It can have its own worker pool, timeouts and resource limits.  It needs its own protected-resource URL and token audience, because the verifier compares the audience with `MCP_URL` in full.  Clients then configure a second MCP server and give a second consent, unless the auth layer accepts more than one audience.  `admin_mcp_serve` and the phase 2 mount (SPEC.md section 12) then serve two apps.

The current decision is A.

### 4. How the bridge calls the tools — decided: A

- **A. A worker thread for each call** (`anyio.to_thread.run_sync`, as in the sketch).  The tools stay sync and do not change.  `CODEMODE_MAX_PARALLEL` limits the threads and the database connections.
- **B. Native async tools.**  The tools become `async def` with the async ORM.  The bridge then awaits them directly.  This contradicts decision D6: the admin API is sync only.
- **C. Run the whole snippet in one worker thread**, with a sync bridge.  The inner calls then run one after the other, and `asyncio.gather` gives no parallel calls.

The current decision is A.  The tools are sync, and A needs no change to them.  A snippet can still run its calls in parallel with `asyncio.gather`.  If the tools become native async, the bridge can change to B.  The snippets and the stubs do not change, because they already see async functions.

### 5. Transactions — decided: A

- **A. Each inner call is its own unit**, as today.  The snippet can read data from different points in time.
- **B. One transaction for the whole run** (for example `REPEATABLE READ`, read only).  The snippet then reads one consistent snapshot.  With the worker threads of decision 4, all inner calls must then share one connection, so the calls run one after the other.

The current decision is A.  It keeps parallel calls, and the tool description tells the agent that the calls do not read one snapshot.

### 6. The `CODEMODE_*` settings and their defaults — decided

The keys are flat keys of `ADMIN_FASTMCP`, like the existing ones: `CODEMODE_ENABLED`, `CODEMODE_TIMEOUT`, `CODEMODE_MAX_CALLS`, `CODEMODE_MAX_PARALLEL`, `CODEMODE_MAX_RESULT_BYTES` and `CODEMODE_MAX_MEMORY_BYTES`.

Code mode is off by default, even with the extra installed: a deployment turns it on explicitly.  The defaults are 30 seconds, 100 calls, 4 parallel calls, 64 KiB of result and 64 MiB of sandbox memory.  With worker threads (decision 4), each parallel call holds one database connection, so the default `CODEMODE_MAX_PARALLEL` is small, and a consumer raises it with its connection pool.  Monty's recursion depth keeps its default (1000), and its suspension budget follows `CODEMODE_MAX_CALLS`.

### 7. A result that is too large — decided: A

- **A. Refuse it** with an error that tells the agent to reduce the result in the snippet.
- **B. Truncate it** and add a marker that tells the agent.

The current decision is A: no silently partial data.  The limit covers the `print` output too.

### 8. What the span and the audit log record — decided: B

- **A. A hash of the snippet only.**  The snippet can contain search terms and personal data.
- **B. The full snippet, with redactions.**  This makes it easier to debug, and it shows which tool combinations agents use.

The current decision is B.  Direct calls record no arguments today: `instrument()` keeps the tool, the model, the user, the client, the outcome and the duration.  `REDACT_FIELDS` hides result values by field name, so it cannot apply to free-form code.  Code mode therefore brings its own redaction, which keeps the structure of the snippet and drops its data:

- The server parses the snippet with `ast` and replaces each string and bytes literal, the constant parts of f-strings included, with `"[redacted]"`.  Search terms, primary keys and filter values are strings, so they go.
- Two kinds of strings stay, because they name the schema and not the data: dict keys (field names and lookups such as `"status__in"`), and the value of a `model=` argument (the span of each inner call records the model anyway).
- Numbers, booleans and `None` stay.  They are mostly page sizes and limits.  A numeric primary key also stays; the tools take primary keys as strings, so a snippet rarely writes one as a number.
- The server records `ast.unparse()` of the result, so comments, which can hold anything, go too.
- The record also keeps a hash of the original snippet, to group the runs of the same snippet.  A snippet that does not parse keeps only its hash.

The redacted snippet goes into an attribute of the run's span and into the `extra` of its audit record.

### 9. Who owns the Monty worker processes — decided: A

- **A. A pool for each run.**  It costs about 10 ms per run.  Nothing outlives the run, nothing joins the ASGI lifespan, and no state can pass from one user's run to another's.
- **B. One pool for the app**, started and stopped with the ASGI lifespan, in `admin_mcp_serve` and in the mounted app.  It saves the start-up cost, but the pool belongs to one event loop, and its workers serve many runs.

The current decision is A.  A shared pool can come later, if the start-up cost shows.

### 10. What `CODEMODE_TIMEOUT` bounds — decided: the whole run

Monty's duration limits count only the time the sandbox computes: the clock stops while the snippet waits on the host, so a snippet of slow queries never reaches them.  `anyio.fail_after(CODEMODE_TIMEOUT)` bounds the whole run, tool calls included, and Monty's `max_feed_duration_secs` stays as a guard against a snippet that only computes.  A query that runs when the deadline passes cannot be stopped (a thread cannot be killed); it finishes in the background, and the run does not wait for it.

## Steps (after the decisions)

1. Add the `codemode` extra, and confirm the API of `pydantic-monty` and its supported Python subset (`async`/`await`, `asyncio.gather`, comprehensions, `dict` and `list` methods).
2. Add the `CODEMODE_*` keys to `conf.DEFAULTS` and its validation to `checks.py`.
3. Write the stub generator from `all_tools()` and `declared()`.
4. Write the bridge and `run_code`, with the limits, and register it in `build_server()`.  When `CODEMODE_ENABLED` is on, `build_server()` also appends the code mode sentence to the server instructions.
5. Verify that `current_user()` returns the token's user inside the inner calls.
6. Nest the inner spans under the span of the run, and record the redacted snippet and its hash (decision 8).
7. Update SPEC.md (sections 3, 4, 9 and 14) and the README.
