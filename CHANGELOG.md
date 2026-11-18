# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Every read-only tool carries the MCP hint `readOnlyHint`, not only `admin_run_code`: the generic reads, `admin_autocomplete`, and the declared tools without `write=True`.  Clients that honor the hint can run them without asking for approval on each call.

## [0.3.2] - 2026-11-17

### Fixed

- The description of `admin_run_code` says which modules the sandbox has, and that its `asyncio` has only `gather`, `sleep` and `run`.  It also says the server queues calls past `CODEMODE_MAX_PARALLEL`, so a snippet need not throttle them.  Agents read that limit as theirs to enforce and reached for `asyncio.Semaphore`, which the sandbox lacks, so the snippet failed its type check.
- The function stubs in that description carry each tool's whole docstring, not its first line, so the agent sees the argument formats and the shape of each answer.  First lines that ran on into a second line no longer stop mid-sentence.

## [0.3.1] - 2026-11-17

### Fixed

- A `ToolRefusalError` no longer reaches Sentry as an error event.  Sentry's own MCP integration, which turns itself on when `mcp` is installed, reported every exception a tool raised; a global event processor, registered when the app loads, now drops the events of refusals.

### Security

- The snippet that `admin_run_code` records has its dict keys redacted too, since a key can be data as much as a value, and keeps a `model=` value only when it names an installed model.  The run's span and audit record also list the tools the snippet called (`mcp_tools`).
- The Logfire span of a tool call keeps the type of the exception that ended it (`mcp.error_type`), not its message nor its traceback, which quote the caller's data (a pk, a filter value, the traceback of a snippet, which quotes its lines as written).

## [0.3.0] - 2026-11-17

### Added

- Code mode, in the new `codemode` extra (`django-admin-fastmcp[codemode]`).  A new tool, `admin_run_code`, runs a short Python snippet in a [pydantic-monty](https://github.com/pydantic/monty) sandbox.  The snippet calls the admin tools as async functions, and only its final value goes back to the agent.  A search followed by one call per result becomes a single tool call, and the intermediate results stay on the server.  Each call from the snippet passes the same permission checks and writes the same spans and audit records as a direct call.  Only the read tools are available in the sandbox; the write tools stay direct calls.  The tool is off by default: the server registers it when the extra is installed and `CODEMODE_ENABLED` is on, and the server instructions then tell agents to prefer it for every read that takes more than one tool call, and that it is read-only.  `CODEMODE_TIMEOUT`, `CODEMODE_MAX_CALLS`, `CODEMODE_MAX_PARALLEL`, `CODEMODE_MAX_RESULT_BYTES` and `CODEMODE_MAX_MEMORY_BYTES` bound each run.  The run's span and audit record keep the snippet with its string literals redacted.  System check `E008` reports `CODEMODE_ENABLED` without the extra.  See [CODEMODE.md](CODEMODE.md).
- `instrument()` accepts async tool functions, and `tracing.annotate()` adds fields to the span and the audit record of the current tool call.
- Django 6.1 support.

### Removed

- Support for Django 5.0 and 5.1, which reached their end of life in April 2025 and December 2025.  The package now requires Django 5.2 or later.

## [0.2.3] - 2026-11-12

### Fixed

- Refusals and invalid calls (permission denials, objects not found, models not exposed, unknown fields, invalid filters, failed validation, and so on) raise `ToolRefusalError`, a `ToolError` that FastMCP logs at INFO.  Before, FastMCP logged each of them at ERROR on `fastmcp.server.server`, and Sentry's logging integration reported them as errors.  Unexpected exceptions in a tool still log at ERROR and reach Sentry.

## [0.2.2] - 2026-11-11

### Fixed

- A declared tool resolves its type hints in the module of the `ModelAdmin`.  With `from __future__ import annotations` the hints were strings, and the server failed to build its input schema with a `NameError` on the first name the library's own module did not import (`UUID`, a model class).

## [0.2.1] - 2026-11-11

### Fixed

- `get_object`, `search_objects` and declared tools that return a model instance serialize a `JSONField` value as JSON.  Before, dicts and lists went through `str()` and reached the agent as their Python repr (`{'ok': True}`).

## [0.2.0] - 2026-11-11

### Added

- Declared tools.  A `ModelAdmin` method decorated with `@mcp_tool` becomes an MCP tool, next to the model it acts on, the way `@admin.action` declares an action.  The method takes `self`, the synthetic admin `request` and its own typed parameters, which become the input schema; its docstring is the description.  It returns a model instance (serialized and logged as a change), a dict, or `None`.  A tool reads by default and needs the view permission on the model; `write=True` gives it the gates of a generic write: `WRITABLE_MODELS` and the change permission.  Wire name `admin_<app_label>_<model_name>_<tool>`; `describe_model` lists the tools the caller may run under `tools`; `DISABLED_TOOLS` removes one.  For models whose rows change only through such tools (a tree, an event-sourced aggregate).
- `mcp_generic_writes = False` on a `ModelAdmin` refuses `create_object`, `update_object` and `delete_object` for that model while its declared tools keep working.
- `instrument(name, fn, model=...)` tags the spans and the audit line of a declared tool with its model.

## [0.1.3] - 2026-09-30

### Fixed

- Close stale Django database connections before and after each tool call and each token verification ([#1](https://github.com/edelvalle/django-admin-fastmcp/issues/1)).  FastMCP never fires Django's request signals, so each worker thread kept its first connection forever: a broken connection stayed broken after a database restart, `CONN_MAX_AGE` and `CONN_HEALTH_CHECKS` had no effect, and pooled connections never returned to the pool.

## [0.1.2] - 2026-09-29

### Added

- `LAST_USED_THROTTLE_MINUTES` setting (default `1`): the minimum gap between `last_used_at` writes on token verification.  `0` or a negative value turns the write off, so a server with no write tools can run against a read-only database replica.  System check `E007` rejects values that are not an `int`.

## [0.1.1] - 2026-09-03

First release.

### Changed

- One permission system. The `write_via_mcp` permission and the
  `admin:write` scope are gone. Writes are gated by `WRITABLE_MODELS`
  (deployment-level, bans whole models such as an event log) and by the
  user's own admin permissions, nothing else. Every grant carries the
  single `admin` scope.

### Added

- Observability (`tracing.py`), mirroring djhtmx: optional Sentry and
  Logfire extras. Every tool call gets a span or transaction tagged with
  tool, model, user, client, and outcome, per-tool counters and duration
  distributions, exception capture for real errors, and one structured log
  record per call on the `django_admin_fastmcp` logger (denials at
  WARNING). New settings: `ENABLE_SENTRY_TRACING`,
  `ENABLE_LOGFIRE_TRACING`.

- The OAuth 2.1 authorization server: dynamic client registration, PKCE
  consent flow riding the admin session, token exchange with refresh
  rotation, and revocation.
- The token verifier and `RemoteAuthProvider` wiring for the MCP server.
- The eleven tools: `list_models`, `describe_model`, `search_objects`,
  `get_object`, `object_history`, `recent_actions`, `create_object`,
  `update_object`, `delete_object`, `run_action`, and `autocomplete`.
- Exposure filtering, field redaction, admin-metadata serialization, the
  synthetic request with a message sink, system checks, and the
  `admin_mcp_serve` command.
- The permission-matrix test suite from `SPEC.md` section 11.
