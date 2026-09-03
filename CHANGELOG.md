# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
