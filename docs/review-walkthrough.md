# Review: django-admin-fastmcp, the whole implementation

**Scope:** the entire repository at its current state (the seven initial
commits plus the write-gate simplification). 58 files: about 3,200 lines of
Python across package, test project, and tests, plus configuration, docs,
and generated files. The coverage table at the end reconciles against the
tree.

**Status:** implemented through milestone M2 of `SPEC.md` (reads, writes,
actions, and the OAuth flow). M3 (CI matrix, PyPI release, ASGI mount
recipe) remains.

**How to read this:** each section explains one part in prose, then shows
that part's code. Sections build on each other, from fundamentals to
specifics. A reader who stops after the prose of each section still
understands the system.

## Summary

The package exposes the Django admin as an MCP server. It adds no permission
system and no data path of its own: every authorization question goes to the
`ModelAdmin` that already answers it in the admin UI, and every write goes
through the admin's own form, `save_model`, and `LogEntry`. Authentication is
OAuth 2.1 with PKCE, with the Django site itself as the authorization server,
so a user authorizes an MCP client in the browser where the admin session
cookie already identifies them. The design has one load-bearing rule, stated
in `SPEC.md` and repeated in code: **fail closed**. Every unresolved lookup,
missing registration, unknown field, unknown action, and malformed token
denies the call.

The implementation is three layers. The foundation layer (settings, errors,
exposure, synthetic request, serialization, checks) defines what exists and
who may see it. The identity layer (models, OAuth views, verifier) decides
who is calling. The tool layer (resolve plus eleven tools) does the work,
asking the admin for permission at every step.

## Decisions on record

Numbered decisions D1 to D8 live in `SPEC.md` section 14. The ones that
shaped the code, plus the ones made during implementation:

- **Delegate all authorization to `ModelAdmin`** (D-spec 1). Rejected: a
  parallel permission table. It would drift from the admin and rot.
- **All writes through the admin form path** (D-spec 2). Rejected: direct
  `Model.objects.create/update`. It would skip validation, `save_model`
  overrides, and the audit trail.
- **OAuth 2.1 code + PKCE, Django as the authorization server** (D3).
  Rejected: manually minted bearer tokens (the original spec draft) because
  setup required copying a secret out of the admin. Rejected: session-cookie
  auth on the MCP transport, because it cannot serve a remote client and
  drags CSRF into the protocol. The cookie now appears only in the browser
  consent step, where Django already handles it.
- **One permission system, Django's.** Writes are gated by `WRITABLE_MODELS`
  (deployment-level: leave an event-log model out and nothing can write it,
  whoever calls) and by the user's own admin permissions per call. Rejected:
  an earlier design with a `write_via_mcp` permission and an `admin:write`
  scope, dropped for adding a second permission system on top of the one the
  admin already has. Every grant carries the single `admin` scope.
- **Eleven generic tools, not per-model tools** (D7). 150 registered models
  times four operations is an unusable catalogue for any MCP client.
- **Sync `def` tools** (D6). The admin API is sync-only. FastMCP runs sync
  tools in a worker thread, so the ORM is legal there. `async def` would
  force `sync_to_async` around every admin call and buy nothing.
- **`MCP_URL` is the single source of truth** for the served path, the
  advertised OAuth resource, and the token audience. Rejected: separate
  path and audience settings. When they drifted apart during development,
  every call answered an unexplained 401. `test_discovery.py` pins the
  invariant and `checks.py` refuses a pathless URL at startup.
- **Relative OAuth urlconf; the project picks the prefix.** Only the
  RFC 8414 metadata document is root-fixed, so it ships in its own
  `well_known_urls.py`. Rejected: one urlconf with hardcoded absolute
  paths, which would dictate URL layout to consumers.
- **Token wire format `damf_<prefix>_<secret>` with a hex prefix.** The
  prefix is looked up, the secret is hash-compared. Hex because
  `token_urlsafe` can emit `_`, which corrupted the format during testing
  (a real bug the suite caught). The secret keeps `token_urlsafe`; the
  parser splits with `maxsplit=2`.
- **Salted SHA-256 for token hashes, not a password hasher.** The secret is
  32 random bytes, so brute force is hopeless anyway. A slow hasher would
  add latency to every MCP call.
- **A replayed authorization code revokes the grant it minted** (OAuth 2.1
  guidance). Rejected: only refusing the replay, which would leave a
  possibly stolen grant alive.
- **Refresh tokens rotate on every use.** A replayed refresh token fails,
  and the legitimate client still holds the new one.
- **One error message for "missing" and "hidden"** rows. `get_object` on a
  pk outside `get_queryset(request)` reads exactly like a pk that never
  existed, so a caller cannot probe for hidden rows.
- **Redaction beats `mcp_fields`.** Listing a password field in the
  allowlist does not reveal it. Only editing `REDACT_FIELDS` does.
  Filtering and ordering by redacted fields is also refused, closing the
  filter-oracle side channel.
- **`impersonate()` as an explicit test hook** in `auth.py`. The in-memory
  MCP transport carries no HTTP headers, so tests need a way to be someone.
  Rejected: monkeypatching `current_user` per test, which hides the seam.
  The hook is a documented contextvar that production code never sets.
- **zuban instead of the spec's `ty`** for type checking, matching the
  tooling of django-datalog and djhtmx, with the same lenient posture
  toward Django's dynamically typed surface.

## Walkthrough

## Part I: Foundations

### 1. Packaging and tooling contract

`pyproject.toml` defines the package: hatchling build, dynamic version from
`__init__.py`, runtime dependencies `django>=5.0` and `fastmcp>=3.4,<4`
(pinned per SPEC section 2.1 so the 4.x upgrade is a dependency bump, not a
rewrite). Dependency groups follow djhtmx's layout. Ruff and zuban read
their configuration from here; pytest points at the test project's settings.
The Makefile follows django-datalog's `help` style with djhtmx's `.envrc`
loading. Reviewer note: the `[tool.mypy]` block disables error codes that
misfire on Django's dynamic surface; the comment records which and why.

**`pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "django-admin-fastmcp"
dynamic = ["version"]
description = "Expose the Django admin as an MCP server, gated by each user's own admin permissions"
readme = "README.md"
license = "MIT"
requires-python = ">=3.11"
authors = [
    { name = "Eddy Ernesto del Valle Pino", email = "eddy@edelvalle.me" },
]
keywords = [
    "django",
    "admin",
    "mcp",
    "fastmcp",
    "model-context-protocol",
    "ai",
    "agents",
]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Environment :: Web Environment",
    "Framework :: Django",
    "Framework :: Django :: 5.0",
    "Framework :: Django :: 5.1",
    "Framework :: Django :: 5.2",
    "Framework :: Django :: 6.0",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Operating System :: OS Independent",
    "Programming Language :: Python",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Programming Language :: Python :: 3.14",
    "Topic :: Internet :: WWW/HTTP",
    "Topic :: Software Development :: Libraries :: Application Frameworks",
    "Topic :: Software Development :: Libraries :: Python Modules",
]
dependencies = [
    "django>=5.0",
    "fastmcp>=3.4,<4",
]

[project.urls]
Homepage = "https://github.com/edelvalle/django-admin-fastmcp"
Repository = "https://github.com/edelvalle/django-admin-fastmcp.git"
Issues = "https://github.com/edelvalle/django-admin-fastmcp/issues"
Changelog = "https://github.com/edelvalle/django-admin-fastmcp/blob/main/CHANGELOG.md"

[dependency-groups]
test = [
    "pytest~=8.4",
    "pytest-django~=4.13",
    "pytest-cov",
]
typing = [
    # zuban reads the standard [tool.mypy] configuration below.
    "zuban>=0.9",
]
lint = [
    "ruff",
]
devtools = [
    "uvicorn[standard]",
]
dev = [
    { include-group = "test" },
    { include-group = "typing" },
    { include-group = "lint" },
    { include-group = "devtools" },
]

[tool.hatch.version]
path = "django_admin_fastmcp/__init__.py"

[tool.hatch.build.targets.sdist]
include = [
    "/django_admin_fastmcp",
    "/README.md",
    "/CHANGELOG.md",
    "/LICENSE",
]

[tool.hatch.build.targets.wheel]
packages = ["django_admin_fastmcp"]

[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "tests.project.settings"
pythonpath = ["."]
addopts = "-vv --ff --maxfail=1"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = [
    "E",   # pycodestyle errors
    "W",   # pycodestyle warnings
    "F",   # pyflakes
    "I",   # isort
    "B",   # flake8-bugbear
    "C4",  # flake8-comprehensions
    "UP",  # pyupgrade
    "S",   # bandit (security)
    "SIM", # simplifications
    "RUF", # ruff-specific rules
]
ignore = [
    "S101",   # asserts are fine, we use them for invariants
    "TRY003", # long messages in exceptions are fine
    "C901",   # complex functions
]

[tool.ruff.lint.per-file-ignores]
"__init__.py" = ["F401"]
# S105/S106: hardcoded secrets are test fixtures
"tests/**" = ["S105", "S106"]
"**/models.py" = ["RUF012"]
"**/migrations/**" = ["RUF012", "E501"]

[tool.ruff.format]
quote-style = "double"

# Zuban (mypy-compatible type checker) reads this section.
# Kept lenient: Django models are dynamically typed.
[tool.mypy]
python_version = "3.11"
files = ["django_admin_fastmcp"]
exclude = '/migrations/'
ignore_missing_imports = true
follow_imports = "silent"
check_untyped_defs = false
disallow_untyped_defs = false
warn_unused_ignores = false
# Noise against Django's dynamically typed surface: request._messages,
# user.is_staff on AbstractBaseUser, Field vs ForeignObjectRel unions,
# manager methods the stubs miss. Same posture as django-datalog.
disable_error_code = [
    "attr-defined",
    "union-attr",
    "arg-type",
    "index",
    "misc",
    "var-annotated",
    "annotation-unchecked",
]

[tool.coverage.run]
source = ["django_admin_fastmcp"]
omit = [
    "*/migrations/*",
]

[tool.coverage.report]
show_missing = true
exclude_also = [
    "pragma: no cover",
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
]
```

**`Makefile`**

```text
.PHONY: help bootstrap install sync lock upgrade update test coverage lint ruff typecheck \
        format check migrate makemigrations superuser serve shell clean build publish publish-test check-dist

PATH := $(HOME)/.local/bin:$(PATH)
SHELL := /bin/bash
PROJECT_NAME := django_admin_fastmcp
PYTHON_VERSION ?= 3.14
MANAGE := tests/project/manage.py

# Load .envrc when inside Emacs, Claude Code, or pi
_LOAD_ENVRC :=
ifdef INSIDE_EMACS
    _LOAD_ENVRC := 1
endif
ifdef CLAUDECODE
    _LOAD_ENVRC := 1
endif
ifeq ($(LLM_AGENT_PUPPETEER),pi)
    _LOAD_ENVRC := 1
endif
ifdef _LOAD_ENVRC
    ifneq (,$(wildcard .envrc))
        export BASH_ENV := $(CURDIR)/.envrc
        _ := $(shell bash -c 'set -a; source $(CURDIR)/.envrc 2>/dev/null && env | grep -E "^[A-Za-z_][A-Za-z0-9_]*=" | grep -v "^SHELL=" | grep -v "^MAKEFLAGS=" | grep -v "^MFLAGS=" | grep -v "^MAKEFILE_LIST=" > $(CURDIR)/.envrc.make.tmp')
        -include .envrc.make.tmp
    endif
endif

UV ?= uv
RUN ?= $(UV) run

help: ## Show this help message
	@echo "django-admin-fastmcp development commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Pin the Python version for uv
	@$(UV) python pin $(PYTHON_VERSION)

install: bootstrap ## Install all dependencies (frozen when the lock allows it)
	@$(UV) sync --frozen || $(UV) sync

sync: bootstrap ## Install from the lock file, fail when it is stale
	@$(UV) sync --frozen

lock: bootstrap ## Refresh the lock file
	@$(UV) sync

upgrade update: bootstrap ## Upgrade every dependency and refresh the lock file
	@$(UV) sync -U

test: ## Run the test suite (the permission matrix)
	@$(RUN) pytest

coverage: ## Run the tests with coverage
	@$(RUN) pytest --cov --cov-report=term

lint: ## Check code style without changing files
	@$(RUN) ruff check $(PROJECT_NAME)/ tests/
	@$(RUN) ruff format --check $(PROJECT_NAME)/ tests/

ruff: ## Run the ruff linter only
	@$(RUN) ruff check $(PROJECT_NAME)/ tests/

typecheck: ## Run the zuban type checker
	@$(RUN) zuban check

format: ## Auto-format the code (includes import sorting)
	@$(RUN) ruff check --fix $(PROJECT_NAME)/ tests/
	@$(RUN) ruff format $(PROJECT_NAME)/ tests/

check: format lint typecheck test ## Format, lint, typecheck, and test

migrate: ## Apply migrations in the test project
	@$(RUN) python $(MANAGE) migrate

makemigrations: ## Create migrations from model changes
	@$(RUN) python $(MANAGE) makemigrations

superuser: ## Create a superuser in the test project
	@$(RUN) python $(MANAGE) createsuperuser

serve: ## Run the MCP server against the test project
	@$(RUN) python $(MANAGE) admin_mcp_serve --port 8765

shell: ## Open a Django shell in the test project
	@$(RUN) python $(MANAGE) shell

clean: ## Remove build artifacts and caches
	@rm -rf build/ dist/ *.egg-info/ .ruff_cache/ .pytest_cache/ .coverage htmlcov/
	@find . -path ./.venv -prune -o -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -path ./.venv -prune -o -name "*.pyc" -delete 2>/dev/null || true

build: clean ## Build the sdist and wheel
	@$(UV) build

publish-test: build ## Publish to TestPyPI
	@$(UV) publish --index-url https://test.pypi.org/simple/

publish: build ## Publish to PyPI (requires credentials in .envrc)
	@$(UV) publish

check-dist: build ## Build and list the distribution files
	@ls -la dist/
```

Support files: `MANIFEST.in` (sdist hygiene), `.gitignore`, `.envrc`
(venv activation plus a commented publish-credential slot, gitignored),
`LICENSE` (MIT), `CHANGELOG.md` (Keep-a-Changelog), `README.md` (consumer
documentation derived from the spec), and `SPEC.md` (the full
specification, updated as decisions changed). The prose files are reviewed
for consistency with the code but not reproduced here.

### 2. The settings surface: one accessor, explicit defaults

Every knob is read through `conf.get(key)`, never from `django.conf.settings`
directly. `get` raises `KeyError` on a key outside `DEFAULTS`, which catches
package-internal typos immediately; consumer typos are caught by the system
checks (section 7). The two URL helpers exist because FastMCP composes the
advertised resource from an origin plus a mount path: `mcp_origin()` strips
the path so it is never doubled, and `mcp_path()` normalizes the slashes
because Starlette treats `/admin/mcp` and `/admin/mcp/` as different routes.

**`django_admin_fastmcp/conf.py`**

```python
"""One accessor over the ADMIN_FASTMCP settings dict (SPEC.md section 3).

`get(key)` reads a knob from the consumer's `ADMIN_FASTMCP` dict, with the
defaults below. It raises `KeyError` on an unknown key, which catches typos in
this package at development time. Typos in consumer settings are caught by
`checks.py` at startup.
"""

from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

DEFAULTS: dict[str, Any] = {
    "SERVER_NAME": "django-admin",
    "ADMIN_SITE": "django.contrib.admin.site",
    "MODELS": (),
    "EXCLUDE_MODELS": (),
    "WRITABLE_MODELS": (),
    "DISABLED_TOOLS": (),
    "REDACT_FIELDS": ("password", "token", "secret", "api_key", "private_key"),
    "MAX_PAGE_SIZE": 200,
    "MAX_PKS": 1000,
    "ACCESS_TOKEN_TTL_MINUTES": 60,
    "REFRESH_TOKEN_TTL_DAYS": 90,
    # Base URL of the Django site, the OAuth issuer. Used in discovery
    # metadata and advertised by the MCP server as its authorization server.
    "SITE_URL": "http://127.0.0.1:8000",
    # Public URL of the MCP endpoint, the OAuth resource. Tokens are
    # audience-bound to it, and it is the single source of truth for the path
    # the endpoint is served on. The admin owns "/admin/mcp", next to the
    # OAuth endpoints at "/admin/mcp/authorize" and friends.
    "MCP_URL": "http://127.0.0.1:8765/admin/mcp",
}


def get(key: str) -> Any:
    if key not in DEFAULTS:
        raise KeyError(f"unknown ADMIN_FASTMCP key: {key}")
    configured = getattr(settings, "ADMIN_FASTMCP", {})
    return configured.get(key, DEFAULTS[key])


def mcp_origin() -> str:
    """Scheme and host of MCP_URL, without the path.

    FastMCP builds the resource URL it advertises as `base_url` plus the mount
    path, so `base_url` must not already carry the path. A `base_url` that
    includes it makes discovery advertise the path twice, and then the audience
    the client asks for no longer matches the one the verifier expects.
    """
    parts = urlsplit(get("MCP_URL"))
    return f"{parts.scheme}://{parts.netloc}"


def mcp_path() -> str:
    """The path MCP_URL points at, which is the path the endpoint is served on.

    Always leading-slashed and never trailing-slashed, because FastMCP and
    Starlette both treat "/admin/mcp" and "/admin/mcp/" as different routes.
    `checks.py` refuses an MCP_URL with no path at startup.
    """
    return "/" + urlsplit(get("MCP_URL")).path.strip("/")
```

### 3. The denial vocabulary

`ToolError` is FastMCP's own exception: its message reaches the MCP client
verbatim while any other exception stays masked. Two helpers give denials a
consistent shape. `not_found` deliberately uses one message for "does not
exist" and "hidden by get_queryset", so a caller cannot distinguish them.

**`django_admin_fastmcp/errors.py`**

```python
"""ToolError and the denial vocabulary (SPEC.md sections 3 and 5).

Fail closed: every unresolved lookup, missing ModelAdmin, unknown action,
unknown field, and unknown tool denies the call. `ToolError` is FastMCP's own
exception, so the message reaches the MCP client verbatim while everything
else stays masked.
"""

from fastmcp.exceptions import ToolError

__all__ = ["ToolError", "denied", "not_found"]


def denied(reason: str) -> ToolError:
    """A permission denial. Raise the return value."""
    return ToolError(f"denied: {reason}")


def not_found(label: str, pk: str) -> ToolError:
    """An object that does not exist or is hidden by get_queryset.

    One message for both cases, so a caller cannot probe for hidden rows.
    """
    return ToolError(f"not found: {label} with pk {pk!r}")
```

### 4. Exposure: which models exist at all

The default is everything in `admin_site._registry`, because the goal is to
expose what the admin exposes. Three narrowing filters apply (EXCLUDE with
`app.*` wildcards, the MODELS allowlist, `mcp_expose = False` on a
ModelAdmin), and a built-in denylist that settings cannot override: this
package's own models, sessions, and DRF tokens. A token written through MCP
would let an agent escalate outside the audit trail. Field-level policy also
lives here: `mcp_fields` / `mcp_exclude_fields` per ModelAdmin, and substring
redaction that an allowlist entry cannot defeat. Reviewer note:
`is_exposed` returns False for anything unresolvable, which is the
fail-closed rule applied to labels.

**`django_admin_fastmcp/exposure.py`**

```python
"""Which models are visible, which fields are redacted (SPEC.md section 7).

Default: every model in `admin_site._registry`. Narrowed by EXCLUDE_MODELS
(with "app_label.*" support), the MODELS allowlist, and `mcp_expose = False`
on a ModelAdmin. A built-in denylist cannot be overridden: every
`django_admin_fastmcp` model, `sessions.Session`, and `authtoken.Token`.
"""

from functools import cache

from django.apps import apps
from django.contrib.admin import AdminSite, ModelAdmin
from django.db.models import Model
from django.utils.module_loading import import_string

from django_admin_fastmcp import conf

# Written through MCP, these would let an agent escalate outside the audit
# trail. Never exposed, whatever the settings say.
BUILTIN_DENY_APPS = frozenset({"django_admin_fastmcp"})
BUILTIN_DENY_MODELS = frozenset({"sessions.session", "authtoken.token"})


@cache
def admin_site() -> AdminSite:
    return import_string(conf.get("ADMIN_SITE"))


def label_of(model: type[Model]) -> str:
    return f"{model._meta.app_label}.{model.__name__}"


def _normalize(label: str) -> str:
    return label.lower()


def _matches(label: str, patterns) -> bool:
    """True when `label` matches an exact "app.Model" or an "app.*" pattern."""
    norm = _normalize(label)
    app_label = norm.split(".", 1)[0]
    for pattern in patterns:
        pattern = _normalize(pattern)
        if pattern == norm:
            return True
        if pattern.endswith(".*") and pattern[:-2] == app_label:
            return True
    return False


def is_exposed(label: str) -> bool:
    """Fail closed: an unresolvable label is not exposed."""
    try:
        model = apps.get_model(label)
    except (LookupError, ValueError):
        return False
    norm = _normalize(label_of(model))
    if norm.split(".", 1)[0] in BUILTIN_DENY_APPS or norm in BUILTIN_DENY_MODELS:
        return False
    if _matches(norm, conf.get("EXCLUDE_MODELS")):
        return False
    allowlist = conf.get("MODELS")
    if allowlist and not _matches(norm, allowlist):
        return False
    model_admin = admin_site()._registry.get(model)
    if model_admin is None:
        return False
    return getattr(model_admin, "mcp_expose", True) is not False


def exposed_models() -> list[tuple[type[Model], ModelAdmin]]:
    return [
        (model, model_admin)
        for model, model_admin in admin_site()._registry.items()
        if is_exposed(label_of(model))
    ]


def is_writable(label: str) -> bool:
    """Both gates: the model is exposed and named in WRITABLE_MODELS."""
    return is_exposed(label) and _matches(label, conf.get("WRITABLE_MODELS"))


def visible_field_names(model_admin: ModelAdmin, all_names: list[str]) -> list[str]:
    """Apply the per-ModelAdmin `mcp_fields` allowlist and
    `mcp_exclude_fields` denylist to a list of field names."""
    allow = getattr(model_admin, "mcp_fields", None)
    deny = getattr(model_admin, "mcp_exclude_fields", ())
    names = all_names
    if allow is not None:
        names = [n for n in names if n in allow]
    return [n for n in names if n not in deny]


def is_redacted(field_name: str) -> bool:
    """Substring match against REDACT_FIELDS. An explicit `mcp_fields` entry
    does not defeat redaction (SPEC.md section 7)."""
    name = field_name.lower()
    return any(marker in name for marker in conf.get("REDACT_FIELDS"))
```

### 5. The synthetic request

`ModelAdmin` methods take an `HttpRequest`; an MCP call has none. This module
builds the minimal request those methods need: user, method, a real admin
path from `reverse()`, immutable query dicts, an unsaved session, and a
message sink. The sink matters most: admin actions and `save_model`
overrides routinely call `self.message_user(request, ...)`, which raises
`MessageFailure` without message storage on the request. The sink collects
those messages so tools can return them to the caller, which the write and
action tests assert.

**`django_admin_fastmcp/request.py`**

```python
"""admin_request(user, ...) -> HttpRequest (SPEC.md section 6).

An HttpRequest sufficient for ModelAdmin permission and queryset methods.
Carries: user, method (GET for reads, POST for writes), a real admin path
from reverse() so code that inspects request.path resolves sensibly, META
with the host, empty immutable QueryDicts for GET and POST, an unsaved
SessionStore, empty COOKIES, and a message sink.

The message sink matters. Admin actions and save_model overrides routinely
call self.message_user(request, ...), which raises MessageFailure without the
messages framework installed on the request. Collected messages are returned
in the tool result, so an action's own feedback reaches the caller.
"""

from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.http import HttpRequest, QueryDict
from django.urls import NoReverseMatch, reverse


class MessageSink:
    """Quacks like django.contrib.messages storage, collects into a list."""

    def __init__(self):
        self.collected: list[str] = []

    def add(self, level, message, extra_tags=""):
        self.collected.append(str(message))

    def __iter__(self):
        return iter(self.collected)


def admin_request(user, model_admin=None, method: str = "GET") -> HttpRequest:
    try:
        if model_admin is not None:
            opts = model_admin.model._meta
            path = reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")
        else:
            path = reverse("admin:index")
    except NoReverseMatch:
        path = "/admin/"

    request = HttpRequest()
    request.method = method
    request.path = request.path_info = path
    request.META = {"SERVER_NAME": "mcp", "SERVER_PORT": "0", "REMOTE_ADDR": "127.0.0.1"}
    request.GET = QueryDict("", mutable=False)
    request.POST = QueryDict("", mutable=False)
    request.COOKIES = {}
    request.user = user
    request.session = SessionStore()
    request._messages = MessageSink()
    return request


def collected_messages(request: HttpRequest) -> list[str]:
    sink = getattr(request, "_messages", None)
    return list(sink) if isinstance(sink, MessageSink) else []
```

### 6. Serialization from admin metadata

Never `model_to_dict`: it drops non-editable fields including the primary
key, and includes every editable field including password hashes. Both prior
art packages made this exact mistake (SPEC section 10). This walker
serializes concrete fields only, always emits `pk` as a string, shapes
relations as `{"pk", "label"}` so an agent reads names instead of ids, caps
many-to-many at 20 with a total count, flags the caller's readonly fields,
and redacts by field name. Reverse relations are named by `describe_model`
but never expanded here.

**`django_admin_fastmcp/serialize.py`**

```python
"""instance -> dict, from admin metadata (SPEC.md section 7).

Never `model_to_dict`. It drops non-editable fields, including the primary
key, and it includes every editable field, including password hashes.

Serialization walks `model._meta.get_fields()` and produces concrete fields
by value, the primary key always as a string, foreign keys as
`{"pk": ..., "label": str(obj)}`, many-to-many as a capped and counted list
of the same shape, readonly fields flagged, redacted values as
`"[redacted]"`, and an `admin_url`. Reverse relations are not expanded.
"""

import datetime
import decimal
import uuid
from typing import Any

from django.contrib.admin import ModelAdmin
from django.db.models import Field, ForeignKey, ManyToManyField, Model
from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse

REDACTED = "[redacted]"
M2M_CAP = 20


def scalar(value: Any) -> Any:
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal | uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _related_shape(obj: Model | None) -> dict | None:
    if obj is None:
        return None
    return {"pk": str(obj.pk), "label": str(obj)}


def admin_url(instance: Model) -> str | None:
    opts = instance._meta
    try:
        return reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[instance.pk])
    except NoReverseMatch:
        return None


def concrete_fields(model: type[Model]) -> list[Field]:
    return [f for f in model._meta.get_fields() if isinstance(f, Field)]


def serialize_instance(
    instance: Model,
    model_admin: ModelAdmin,
    request: HttpRequest,
) -> dict[str, Any]:
    from django_admin_fastmcp import exposure

    model = type(instance)
    fields = concrete_fields(model)
    names = exposure.visible_field_names(model_admin, [f.name for f in fields])
    readonly = set(model_admin.get_readonly_fields(request, instance))

    data: dict[str, Any] = {}
    for field in fields:
        if field.name not in names:
            continue
        if exposure.is_redacted(field.name):
            data[field.name] = REDACTED
        elif isinstance(field, ManyToManyField):
            related = getattr(instance, field.name).all()
            total = related.count()
            data[field.name] = {
                "items": [_related_shape(obj) for obj in related[:M2M_CAP]],
                "total": total,
            }
        elif isinstance(field, ForeignKey):
            data[field.name] = _related_shape(getattr(instance, field.name))
        else:
            data[field.name] = scalar(getattr(instance, field.name))

    return {
        "pk": str(instance.pk),
        "fields": data,
        "readonly_fields": sorted(readonly & set(names)),
        "admin_url": admin_url(instance),
        "str": str(instance),
    }
```

### 7. Startup checks and app wiring

A bad setting is an error at startup, not a 401 at the first call. E001
catches unknown keys, E002 unresolvable model labels, E003 unknown tool
names (including the classic mistake of writing the wire name
`admin_list_models` instead of the catalogue name `list_models`), E004 an
`ADMIN_SITE` that does not import. E005 and E006 validate the two URLs; the
comment records the failure they prevent. Writable permission models get a
warning (W001), not an error, because that is dangerous rather than broken.
`apps.py` registers the checks by importing the module.

**`django_admin_fastmcp/checks.py`**

```python
"""Startup validation of the ADMIN_FASTMCP settings (SPEC.md section 3).

A bad value is an error, not a warning. Exceptions: auth.Permission and
auth.Group exposed and writable is a warning (SPEC.md section 7), because it
is dangerous rather than broken.
"""

from urllib.parse import urlsplit

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, Warning, register
from django.utils.module_loading import import_string

from django_admin_fastmcp import conf

PERMISSION_MODELS = ("auth.permission", "auth.group")


def _resolves(label: str) -> bool:
    if label.endswith(".*"):
        return apps.is_installed(label[:-2]) or bool(
            [m for m in apps.get_models() if m._meta.app_label == label[:-2]]
        )
    try:
        apps.get_model(label)
    except (LookupError, ValueError):
        return False
    return True


@register()
def check_settings(app_configs, **kwargs):
    from django_admin_fastmcp.server import all_tools

    errors = []
    configured = getattr(settings, "ADMIN_FASTMCP", {})

    for key in configured:
        if key not in conf.DEFAULTS:
            errors.append(
                Error(
                    f"unknown ADMIN_FASTMCP key: {key!r}",
                    hint=f"known keys: {sorted(conf.DEFAULTS)}",
                    id="django_admin_fastmcp.E001",
                )
            )

    for setting in ("MODELS", "EXCLUDE_MODELS", "WRITABLE_MODELS"):
        for label in conf.get(setting):
            if not _resolves(label):
                errors.append(
                    Error(
                        f"ADMIN_FASTMCP[{setting!r}] names {label!r}, "
                        "which is not an installed model",
                        id="django_admin_fastmcp.E002",
                    )
                )

    known_tools = set(all_tools())
    for name in conf.get("DISABLED_TOOLS"):
        if name not in known_tools:
            errors.append(
                Error(
                    f"ADMIN_FASTMCP['DISABLED_TOOLS'] names {name!r}, "
                    f"which is not a tool. Tools: {sorted(known_tools)}",
                    id="django_admin_fastmcp.E003",
                )
            )

    try:
        import_string(conf.get("ADMIN_SITE"))
    except ImportError:
        errors.append(
            Error(
                f"ADMIN_FASTMCP['ADMIN_SITE'] does not import: {conf.get('ADMIN_SITE')!r}",
                id="django_admin_fastmcp.E004",
            )
        )

    # MCP_URL is the path the endpoint is served on and the audience every
    # token is bound to. A URL with no path would advertise the whole origin
    # as the protected resource, and the served path would collapse to "/".
    mcp_parts = urlsplit(conf.get("MCP_URL"))
    if (
        mcp_parts.scheme not in ("http", "https")
        or not mcp_parts.netloc
        or not mcp_parts.path.strip("/")
    ):
        errors.append(
            Error(
                f"ADMIN_FASTMCP['MCP_URL'] must be an absolute http(s) URL with a path, "
                f"got {conf.get('MCP_URL')!r}",
                hint="for example https://admin.example.com/admin/mcp",
                id="django_admin_fastmcp.E005",
            )
        )

    site_parts = urlsplit(conf.get("SITE_URL"))
    if site_parts.scheme not in ("http", "https") or not site_parts.netloc:
        errors.append(
            Error(
                f"ADMIN_FASTMCP['SITE_URL'] must be an absolute http(s) URL, "
                f"got {conf.get('SITE_URL')!r}",
                hint="it is the OAuth issuer, the Django site that serves the admin login",
                id="django_admin_fastmcp.E006",
            )
        )

    writable = [w.lower() for w in conf.get("WRITABLE_MODELS")]
    for label in PERMISSION_MODELS:
        if label in writable:
            errors.append(
                Warning(
                    f"{label} is writable over MCP. An agent that can grant "
                    "permissions can escape the permission model.",
                    hint="remove it from ADMIN_FASTMCP['WRITABLE_MODELS']",
                    id="django_admin_fastmcp.W001",
                )
            )
    return errors
```

**`django_admin_fastmcp/apps.py`**

```python
from django.apps import AppConfig


class DjangoAdminFastmcpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_admin_fastmcp"
    verbose_name = "Admin MCP"

    def ready(self):
        # Import registers the system checks (SPEC.md section 3).
        from django_admin_fastmcp import checks  # noqa: F401
```

## Part II: Identity

### 8. Token primitives and the wire format

Two token kinds share one shape: `damf_<prefix>_<secret>` for access,
`damfr_...` for refresh. The prefix is a database lookup key; the secret is
compared only as a salted SHA-256, in constant time. The prefix is hex
because it sits between the underscores; `token_urlsafe` can emit `_` and
did, which broke parsing until the refresh-rotation test caught it. The
secret keeps the larger `token_urlsafe` alphabet and the parser splits with
`maxsplit=2`. `split_wire_token` returns `None` on any malformed value and
every caller treats `None` as denial.

**`django_admin_fastmcp/models.py`** (excerpt)

```python
"""OAuth storage: McpClient, McpToken, McpAuthorizationCode (SPEC.md section 8).

Only prefixes and salted SHA-256 hashes of secrets are stored, so a leaked
database row cannot be replayed. Secrets are 32 random bytes from
`secrets.token_urlsafe`; the entropy is high, so a single salted SHA-256 is
the right primitive and a slow password hasher would only add latency.

Wire formats: access tokens are "damf_<prefix>_<secret>", refresh tokens are
"damfr_<prefix>_<secret>".
"""

import hashlib
import secrets
from datetime import timedelta
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

ACCESS_TOKEN_MARKER = "damf"  # noqa: S105 - a wire-format tag, not a secret
REFRESH_TOKEN_MARKER = "damfr"  # noqa: S105

# One scope: "act in the admin as this user". What a grant may do is decided
# by the user's own admin permissions plus WRITABLE_MODELS, not by scopes.
SCOPE_ADMIN = "admin"


def make_secret() -> str:
    return secrets.token_urlsafe(32)


def make_prefix() -> str:
    # Hex on purpose: the prefix sits between the two underscores of the wire
    # format, so it must never contain one. token_urlsafe can emit "_".
    return secrets.token_hex(6)


def hash_secret(secret: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}{secret}".encode()).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def split_wire_token(wire: str) -> tuple[str, str, str] | None:
    """Split "damf_<prefix>_<secret>" into (marker, prefix, secret).

    Returns None on a malformed value. The caller denies.
    """
    # maxsplit, because the secret itself may contain underscores
    parts = wire.split("_", 2)
    if len(parts) != 3:
        return None
    marker, prefix, secret = parts
    if marker not in (ACCESS_TOKEN_MARKER, REFRESH_TOKEN_MARKER) or not prefix or not secret:
        return None
    return marker, prefix, secret
```

### 9. Storage: client, grant, code

Three models. `McpClient` is a dynamically registered OAuth client (RFC
7591). `McpToken` is one grant: user x client, holding the hashes of the
current access and refresh pair; `issue` creates it and `rotate` renews both
tokens in place, so a replayed refresh token dies naturally. Plaintext
tokens exist only in return values, never in a field. `McpAuthorizationCode`
is stored by hash too, expires in 60 seconds, burns on first use, and keeps
a foreign key to the grant it minted so a replay can revoke it. Reviewer
note: `revoked_at`/`used_at` timestamps instead of booleans double as audit
data.

**`django_admin_fastmcp/models.py`** (excerpt)

```python
class McpClient(models.Model):
    """A dynamically registered OAuth client (RFC 7591)."""

    client_id = models.CharField(max_length=48, unique=True, default=make_prefix)
    name = models.CharField(max_length=200)
    redirect_uris = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name or self.client_id


class McpToken(models.Model):
    """One grant: user x client, holding the current token pair.

    The access token expires fast and renews with the refresh token, which
    rotates on every use. A grant acts with its user's own admin
    permissions, never more.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mcp_tokens"
    )
    client = models.ForeignKey(McpClient, on_delete=models.CASCADE, related_name="tokens")
    scopes = models.JSONField(default=list)
    resource = models.CharField(max_length=500, blank=True, default="")
    access_prefix = models.CharField(max_length=12, unique=True, db_index=True)
    access_hash = models.CharField(max_length=64)
    refresh_prefix = models.CharField(max_length=12, unique=True, db_index=True)
    refresh_hash = models.CharField(max_length=64)
    salt = models.CharField(max_length=32)
    access_expires_at = models.DateTimeField()
    refresh_expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} x {self.client}"

    # -- issuing ---------------------------------------------------------

    @classmethod
    def issue(cls, *, user, client, scopes, resource="") -> tuple["McpToken", str, str]:
        """Create a grant. Returns (grant, access_wire, refresh_wire).

        The plaintext tokens exist only in the return value.
        """
        from django_admin_fastmcp import conf

        grant = cls(user=user, client=client, scopes=list(scopes), resource=resource)
        grant.salt = secrets.token_urlsafe(16)[:32]
        access_wire = grant._set_access()
        refresh_wire = grant._set_refresh()
        grant.refresh_expires_at = timezone.now() + timedelta(
            days=conf.get("REFRESH_TOKEN_TTL_DAYS")
        )
        grant.save()
        return grant, access_wire, refresh_wire

    def rotate(self) -> tuple[str, str]:
        """Refresh: new access token, and the refresh token rotates too."""
        access_wire = self._set_access()
        refresh_wire = self._set_refresh()
        self.save(
            update_fields=[
                "access_prefix",
                "access_hash",
                "refresh_prefix",
                "refresh_hash",
                "access_expires_at",
            ]
        )
        return access_wire, refresh_wire

    def _set_access(self) -> str:
        from django_admin_fastmcp import conf

        prefix, secret = make_prefix(), make_secret()
        self.access_prefix = prefix
        self.access_hash = hash_secret(secret, self.salt)
        self.access_expires_at = timezone.now() + timedelta(
            minutes=conf.get("ACCESS_TOKEN_TTL_MINUTES")
        )
        return f"{ACCESS_TOKEN_MARKER}_{prefix}_{secret}"

    def _set_refresh(self) -> str:
        prefix, secret = make_prefix(), make_secret()
        self.refresh_prefix = prefix
        self.refresh_hash = hash_secret(secret, self.salt)
        return f"{REFRESH_TOKEN_MARKER}_{prefix}_{secret}"

    # -- state -----------------------------------------------------------

    def revoke(self):
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class McpAuthorizationCode(models.Model):
    """A single-use authorization code bound to a PKCE challenge.

    Expires in 60 seconds and burns on first use. A reused code revokes the
    grant it minted (SPEC.md section 8).
    """

    LIFETIME: ClassVar[timedelta] = timedelta(seconds=60)

    code_hash = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey(McpClient, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    scopes = models.JSONField(default=list)
    redirect_uri = models.CharField(max_length=500)
    code_challenge = models.CharField(max_length=128)
    resource = models.CharField(max_length=500, blank=True, default="")
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    token = models.ForeignKey(McpToken, on_delete=models.SET_NULL, null=True, blank=True)

    @classmethod
    def mint(cls, *, client, user, scopes, redirect_uri, code_challenge, resource="") -> str:
        """Create a code row. Returns the plaintext code."""
        code = secrets.token_urlsafe(32)
        cls.objects.create(
            code_hash=hashlib.sha256(code.encode()).hexdigest(),
            client=client,
            user=user,
            scopes=list(scopes),
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            resource=resource,
            expires_at=timezone.now() + cls.LIFETIME,
        )
        return code

    @classmethod
    def find(cls, code: str) -> "McpAuthorizationCode | None":
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        return cls.objects.filter(code_hash=code_hash).select_related("client", "user").first()
```

### 10. The authorization server: discovery and registration

`oauth.py` holds the five endpoints as plain Django views. The helpers set
the security posture: redirect URIs must be https or loopback http (CLI
clients like Claude Code listen on `127.0.0.1`) and PKCE is S256 only.
Requested scopes are ignored: every grant carries the single `admin` scope,
because authorization lives in the admin's permissions, not in scopes. The
metadata document derives every URL from the request and
`reverse()`, so it follows whatever mount prefix the project chose.
Registration accepts any client (that is the point of RFC 7591: Claude Code
registers itself), but a client is only a name and redirect list; identity
comes from the user who approves the consent page.

**`django_admin_fastmcp/oauth.py`** (excerpt)

```python
"""The authorization server: Django views (SPEC.md section 8).

OAuth 2.1 authorization-code flow with PKCE. The consent page rides the admin
session cookie in the browser. Enforced, not optional: PKCE with S256 only,
exact `redirect_uri` match with loopback addresses allowed for CLI clients,
codes that expire in 60 seconds and burn on first use, and a reused code
revokes the grant it minted.
"""

import base64
import hashlib
import json
from urllib.parse import urlencode, urlsplit

from django.contrib.auth.views import redirect_to_login
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from django_admin_fastmcp.models import (
    SCOPE_ADMIN,
    McpAuthorizationCode,
    McpClient,
    McpToken,
    constant_time_compare,
    hash_secret,
    split_wire_token,
)

ALL_SCOPES = (SCOPE_ADMIN,)
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")


# -- helpers ---------------------------------------------------------------


def _valid_redirect_uri(uri: str) -> bool:
    parts = urlsplit(uri)
    if parts.scheme == "https":
        return bool(parts.netloc)
    if parts.scheme == "http":
        return parts.hostname in LOOPBACK_HOSTS
    return False


def _pkce_matches(challenge: str, verifier: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return constant_time_compare(challenge, expected)


def _token_error(error: str, description: str = "", status: int = 400) -> JsonResponse:
    body = {"error": error}
    if description:
        body["error_description"] = description
    return JsonResponse(body, status=status)


def _staff_or_login(request: HttpRequest) -> HttpResponse | None:
    """Return a redirect or denial for the browser, or None when staff."""
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), login_url=reverse("admin:login"))
    if not (request.user.is_active and request.user.is_staff):
        return HttpResponseForbidden("A staff account is required to authorize MCP clients.")
    return None


# -- endpoints --------------------------------------------------------------


@require_http_methods(["GET"])
def metadata(request: HttpRequest) -> JsonResponse:
    """Authorization server metadata (RFC 8414)."""
    issuer = request.build_absolute_uri("/").rstrip("/")
    absolute = request.build_absolute_uri
    return JsonResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": absolute(reverse("django_admin_fastmcp:authorize")),
            "token_endpoint": absolute(reverse("django_admin_fastmcp:token")),
            "registration_endpoint": absolute(reverse("django_admin_fastmcp:register")),
            "revocation_endpoint": absolute(reverse("django_admin_fastmcp:revoke")),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(ALL_SCOPES),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def register(request: HttpRequest) -> JsonResponse:
    """Dynamic client registration (RFC 7591). Public clients only, PKCE."""
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _token_error("invalid_client_metadata", "request body is not JSON")

    redirect_uris = payload.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _token_error("invalid_redirect_uri", "redirect_uris is required")
    for uri in redirect_uris:
        if not isinstance(uri, str) or not _valid_redirect_uri(uri):
            return _token_error(
                "invalid_redirect_uri",
                f"{uri!r} is not https and not a loopback http URI",
            )

    client = McpClient.objects.create(
        name=str(payload.get("client_name") or "MCP client")[:200],
        redirect_uris=redirect_uris,
    )
    return JsonResponse(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at.timestamp()),
            "client_name": client.name,
            "redirect_uris": client.redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status=201,
    )
```

### 11. The consent page

`authorize` is the one browser-facing view and the only place the session
cookie matters. Anonymous users go through the normal admin login;
authenticated non-staff get a 403. Client identity errors (unknown client,
unregistered redirect URI) render locally and never redirect, per OAuth 2.1
security guidance: redirecting to an unverified URI would build an open
redirector. Everything else redirects back to the client with an `error`
parameter. The POST branch mints the single-use code. The template extends
the admin's own base so it looks native, and states plainly what approval
means: the client acts with the user's own admin permissions, writes only
where the server allows them, and everything is logged.

**`django_admin_fastmcp/oauth.py`** (excerpt)

```python
@require_http_methods(["GET", "POST"])
def authorize(request: HttpRequest) -> HttpResponse:
    """The login-gated consent page.

    GET shows the consent form. POST (with CSRF protection) mints the code
    and redirects back to the client. Errors in client identity or redirect
    URI render locally and never redirect, per OAuth 2.1 security guidance.
    """
    denial = _staff_or_login(request)
    if denial is not None:
        return denial

    params = request.POST if request.method == "POST" else request.GET
    client = McpClient.objects.filter(client_id=params.get("client_id", "")).first()
    if client is None:
        return HttpResponseBadRequest("unknown client_id")
    redirect_uri = params.get("redirect_uri", "")
    if redirect_uri not in client.redirect_uris:
        return HttpResponseBadRequest("redirect_uri is not registered for this client")

    state = params.get("state", "")

    def _redirect_error(error: str) -> HttpResponseRedirect:
        query = {"error": error}
        if state:
            query["state"] = state
        return HttpResponseRedirect(f"{redirect_uri}?{urlencode(query)}")

    if params.get("response_type") != "code":
        return _redirect_error("unsupported_response_type")
    code_challenge = params.get("code_challenge", "")
    if not code_challenge or params.get("code_challenge_method") != "S256":
        return _redirect_error("invalid_request")

    # Requested scopes are ignored on purpose: there is only one, and what a
    # grant may do is decided by the user's admin permissions, not by scopes.
    resource = params.get("resource", "")

    if request.method == "GET":
        return render(
            request,
            "django_admin_fastmcp/authorize.html",
            {
                "client": client,
                "params": {
                    "response_type": "code",
                    "client_id": client.client_id,
                    "redirect_uri": redirect_uri,
                    "scope": SCOPE_ADMIN,
                    "state": state,
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                    "resource": resource,
                },
            },
        )

    if params.get("decision") != "approve":
        return _redirect_error("access_denied")

    code = McpAuthorizationCode.mint(
        client=client,
        user=request.user,
        scopes=[SCOPE_ADMIN],
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        resource=resource,
    )
    query = {"code": code}
    if state:
        query["state"] = state
    return HttpResponseRedirect(f"{redirect_uri}?{urlencode(query)}")
```

**`django_admin_fastmcp/templates/django_admin_fastmcp/authorize.html`**

```html
{% extends "admin/base_site.html" %}

{% block title %}Authorize {{ client.name }}{% endblock %}

{% block content %}
<h1>Authorize MCP client</h1>
<p>
  <strong>{{ client.name }}</strong> asks to act in the admin as
  <strong>{{ request.user.get_username }}</strong>.
</p>
<ul>
  <li>It can read what you can read in the admin.</li>
  <li>It can create, change, delete, and run actions only where you can,
      and only on models this server accepts writes for.</li>
  <li>Every change it makes is logged in the admin history under your name,
      marked with the client name.</li>
</ul>
<form method="post">
  {% csrf_token %}
  {% for key, value in params.items %}
    <input type="hidden" name="{{ key }}" value="{{ value }}">
  {% endfor %}
  <div class="submit-row">
    <input type="submit" name="decision" value="approve" class="default">
    <input type="submit" name="decision" value="deny">
  </div>
</form>
{% endblock %}
```

### 12. The token endpoint and revocation

`_exchange_code` validates in strict order: code exists, not used, not
expired, client matches, redirect URI matches, PKCE verifier matches. The
used-code branch comes first and revokes the minted grant, because a
replayed code means either the client leaked it or someone raced the client
to it. `_exchange_refresh` re-checks that the user is still active staff, so
deactivating a person cuts their agents off at the next refresh at the
latest (the verifier already cuts them off at the next call). `revoke`
answers 200 unconditionally per RFC 7009, so it cannot be used as an oracle
for which tokens exist.

**`django_admin_fastmcp/oauth.py`** (excerpt)

```python
@csrf_exempt
@require_http_methods(["POST"])
def token(request: HttpRequest) -> JsonResponse:
    """Code and refresh-token exchange."""
    grant_type = request.POST.get("grant_type", "")
    if grant_type == "authorization_code":
        return _exchange_code(request)
    if grant_type == "refresh_token":
        return _exchange_refresh(request)
    return _token_error("unsupported_grant_type", grant_type or "missing grant_type")


def _exchange_code(request: HttpRequest) -> JsonResponse:
    code = McpAuthorizationCode.find(request.POST.get("code", ""))
    if code is None:
        return _token_error("invalid_grant", "unknown code")
    if code.used_at is not None:
        # A replayed code revokes the grant it minted.
        if code.token is not None:
            code.token.revoke()
        return _token_error("invalid_grant", "code already used")
    if code.expires_at < timezone.now():
        return _token_error("invalid_grant", "code expired")
    if request.POST.get("client_id", "") != code.client.client_id:
        return _token_error("invalid_grant", "client_id mismatch")
    if request.POST.get("redirect_uri", "") != code.redirect_uri:
        return _token_error("invalid_grant", "redirect_uri mismatch")
    verifier = request.POST.get("code_verifier", "")
    if not verifier or not _pkce_matches(code.code_challenge, verifier):
        return _token_error("invalid_grant", "PKCE verification failed")

    grant, access_wire, refresh_wire = McpToken.issue(
        user=code.user,
        client=code.client,
        scopes=code.scopes,
        resource=code.resource,
    )
    code.used_at = timezone.now()
    code.token = grant
    code.save(update_fields=["used_at", "token"])
    return _token_response(grant, access_wire, refresh_wire)


def _exchange_refresh(request: HttpRequest) -> JsonResponse:
    wire = request.POST.get("refresh_token", "")
    parts = split_wire_token(wire)
    if parts is None or parts[0] != "damfr":
        return _token_error("invalid_grant", "malformed refresh token")
    _, prefix, secret = parts
    grant = McpToken.objects.filter(refresh_prefix=prefix).select_related("user").first()
    if grant is None:
        return _token_error("invalid_grant", "unknown refresh token")
    if not constant_time_compare(hash_secret(secret, grant.salt), grant.refresh_hash):
        return _token_error("invalid_grant", "refresh token verification failed")
    if grant.is_revoked or grant.refresh_expires_at < timezone.now():
        return _token_error("invalid_grant", "refresh token expired or revoked")
    if not (grant.user.is_active and grant.user.is_staff):
        return _token_error("invalid_grant", "user is no longer staff")
    if request.POST.get("client_id", "") != grant.client.client_id:
        return _token_error("invalid_grant", "client_id mismatch")

    access_wire, refresh_wire = grant.rotate()
    return _token_response(grant, access_wire, refresh_wire)


def _token_response(grant: McpToken, access_wire: str, refresh_wire: str) -> JsonResponse:
    expires_in = int((grant.access_expires_at - timezone.now()).total_seconds())
    return JsonResponse(
        {
            "access_token": access_wire,
            "token_type": "Bearer",
            "expires_in": max(expires_in, 0),
            "refresh_token": refresh_wire,
            "scope": " ".join(grant.scopes),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def revoke(request: HttpRequest) -> HttpResponse:
    """Token revocation (RFC 7009). Always 200, so callers cannot probe."""
    parts = split_wire_token(request.POST.get("token", ""))
    if parts is not None:
        marker, prefix, secret = parts
        field = "access" if marker == "damf" else "refresh"
        grant = McpToken.objects.filter(**{f"{field}_prefix": prefix}).first()
        if grant is not None and constant_time_compare(
            hash_secret(secret, grant.salt), getattr(grant, f"{field}_hash")
        ):
            grant.revoke()
    return HttpResponse(status=200)
```

### 13. Mounting: the project picks the prefix

Two urlconfs. `urls.py` holds the four endpoints with relative paths and a
namespace, so a project mounts them anywhere and the metadata follows,
because every advertised URL comes from `reverse()`. `well_known_urls.py`
holds the one route that cannot move: RFC 8414 fixes the metadata document
at the issuer root. It carries no `app_name` on purpose, because a second
namespace named `django_admin_fastmcp` would collide with the first. The
test project mounts both the intended way; `alt_urls.py` mounts everything
somewhere else entirely and is used by a test to prove nothing is
hardcoded.

**`django_admin_fastmcp/urls.py`**

```python
"""The OAuth endpoints (SPEC.md section 8).

These paths are relative, so the project chooses the prefix:

    path("admin/mcp/", include("django_admin_fastmcp.urls")),

That yields /admin/mcp/authorize and friends, which matches the default
MCP_URL. A project whose admin lives somewhere else mounts them somewhere
else, and nothing in this package needs to know. Every URL the metadata
document advertises comes from `reverse()`, so it follows the prefix you pick.

The authorization-server metadata document is not here. RFC 8414 requires it
at the site root, so it has its own urlconf:

    path("", include("django_admin_fastmcp.well_known_urls")),
"""

from django.urls import path

from django_admin_fastmcp import oauth

app_name = "django_admin_fastmcp"

urlpatterns = [
    path("register", oauth.register, name="register"),
    path("authorize", oauth.authorize, name="authorize"),
    path("token", oauth.token, name="token"),
    path("revoke", oauth.revoke, name="revoke"),
]
```

**`django_admin_fastmcp/well_known_urls.py`**

```python
"""The one route that cannot move (SPEC.md section 8).

RFC 8414 defines the authorization-server metadata document at
`/.well-known/oauth-authorization-server` on the issuer origin. A client
derives that URL from the issuer, so it is not ours to relocate. Mount this at
the site root:

    path("", include("django_admin_fastmcp.well_known_urls")),

No `app_name`: nothing reverses this route by name, and a second namespace
sharing `django_admin_fastmcp` would collide with the endpoints urlconf.
"""

from django.urls import path

from django_admin_fastmcp import oauth

urlpatterns = [
    path(".well-known/oauth-authorization-server", oauth.metadata, name="oauth_metadata"),
]
```

**`tests/project/urls.py`**

```python
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # RFC 8414 fixes this one at the site root.
    path("", include("django_admin_fastmcp.well_known_urls")),
    # The prefix is ours to choose. It matches the path in MCP_URL.
    path("admin/mcp/", include("django_admin_fastmcp.urls")),
    path("admin/", admin.site.urls),
]
```

**`tests/alt_urls.py`**

```python
"""A project that mounts everything somewhere other than /admin/mcp/.

Used by `test_discovery.py` to prove the prefix is the project's choice: the
package hardcodes no path, and the metadata document follows whatever mount
point the project picks.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("django_admin_fastmcp.well_known_urls")),
    path("backoffice/oauth/", include("django_admin_fastmcp.urls")),
    path("backoffice/", admin.site.urls),
]
```

### 14. The verifier and per-call identity

`DjangoAdminTokenVerifier` runs on every MCP request. The sync core is the
real logic (and what unit tests call); the async wrapper exists because
FastMCP's interface is async while the ORM is not. Order of checks: shape,
lookup by prefix, constant-time hash compare, revocation and expiry, the
user still being active staff, and audience: a token bound to a different
resource than `MCP_URL` is refused. `last_used_at` updates at most once per
minute to keep the hot path cheap. The returned `AccessToken` carries the
user pk and client name as claims, which is how the tool layer knows who is
calling. `build_auth` composes the verifier with the RFC 9728 metadata that
tells an unauthenticated client where to go authorize: `base_url` is the
origin only, because FastMCP appends the mount path itself.

`current_user()` resolves identity inside a tool call, re-checking staff
status against the database on every call, so a mid-session deactivation
takes effect immediately. `impersonate` is the explicit test seam for the
in-memory transport; production code never sets it.

**`django_admin_fastmcp/auth.py`**

```python
"""DjangoAdminTokenVerifier, RemoteAuthProvider wiring, current_user() (SPEC.md section 8).

The MCP server side of auth: a FastMCP `RemoteAuthProvider` composes the
verifier with RFC 9728 protected-resource metadata naming the Django site as
the authorization server.
"""

import contextvars
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from fastmcp.server.auth import AccessToken, RemoteAuthProvider, TokenVerifier
from fastmcp.server.dependencies import get_access_token

from django_admin_fastmcp import conf
from django_admin_fastmcp.errors import denied
from django_admin_fastmcp.models import (
    ACCESS_TOKEN_MARKER,
    McpToken,
    constant_time_compare,
    hash_secret,
    split_wire_token,
)

LAST_USED_THROTTLE = timedelta(minutes=1)

# Test hook: the in-memory MCP transport carries no HTTP bearer token, so the
# test suite impersonates a user here. Production calls never set it.
_impersonated: contextvars.ContextVar = contextvars.ContextVar(
    "django_admin_fastmcp_user", default=None
)


class impersonate:
    """Context manager for tests: run tool calls as `user`."""

    def __init__(self, user):
        self.user = user

    def __enter__(self):
        self._token = _impersonated.set(self.user)
        return self

    def __exit__(self, *exc_info):
        _impersonated.reset(self._token)


class DjangoAdminTokenVerifier(TokenVerifier):
    """Verify a wire token against McpToken. Every failure path denies."""

    async def verify_token(self, token: str) -> AccessToken | None:
        return await sync_to_async(self.verify_token_sync)(token)

    def verify_token_sync(self, token: str) -> AccessToken | None:
        parts = split_wire_token(token)
        if parts is None or parts[0] != ACCESS_TOKEN_MARKER:
            return None
        _, prefix, secret = parts
        grant = McpToken.objects.filter(access_prefix=prefix).select_related("user").first()
        if grant is None:
            return None
        if not constant_time_compare(hash_secret(secret, grant.salt), grant.access_hash):
            return None
        now = timezone.now()
        if grant.is_revoked or grant.access_expires_at < now:
            return None
        if not (grant.user.is_active and grant.user.is_staff):
            return None
        expected_resource = conf.get("MCP_URL").rstrip("/")
        if grant.resource and grant.resource.rstrip("/") != expected_resource:
            return None
        if grant.last_used_at is None or now - grant.last_used_at > LAST_USED_THROTTLE:
            grant.last_used_at = now
            grant.save(update_fields=["last_used_at"])
        return AccessToken(
            token=token,
            client_id=str(grant.user.pk),
            scopes=list(grant.scopes),
            expires_at=int(grant.access_expires_at.timestamp()),
            resource=grant.resource or None,
            claims={"user_pk": grant.user.pk, "mcp_client": grant.client.name},
        )


def build_auth() -> RemoteAuthProvider:
    """Compose the verifier with RFC 9728 protected-resource metadata.

    `base_url` is the origin only. FastMCP appends the mount path to it when
    the app is built, so the advertised resource comes out equal to MCP_URL,
    which is what the verifier compares each token's audience against.
    """
    return RemoteAuthProvider(
        token_verifier=DjangoAdminTokenVerifier(),
        authorization_servers=[conf.get("SITE_URL")],
        base_url=conf.mcp_origin(),
        resource_name=conf.get("SERVER_NAME"),
        scopes_supported=["admin"],
    )


def _access_token():
    try:
        return get_access_token()
    except Exception:
        return None


def current_user():
    """The Django user behind the current tool call. Denies when absent.

    Runs inside a sync tool, so the ORM is legal here.
    """
    override = _impersonated.get()
    if override is not None:
        return override
    access_token = _access_token()
    if access_token is None:
        raise denied("no authenticated user on this call")
    claims = access_token.claims or {}
    user = get_user_model().objects.filter(pk=claims.get("user_pk")).first()
    if user is None or not (user.is_active and user.is_staff):
        raise denied("not a staff user")
    return user


def client_name() -> str:
    """The OAuth client name, for LogEntry change messages."""
    if _impersonated.get() is not None:
        return "test"
    access_token = _access_token()
    if access_token is None:
        return "unknown"
    return str((access_token.claims or {}).get("mcp_client", "unknown"))
```

## Part III: The tool surface

### 15. resolve() and the write gate

Every tool resolves its target through one function, in one order: active
staff user, exposed label, registered ModelAdmin, then the admin's own
`has_<action>_permission`. Every failure raises; no branch allows a call
because a lookup returned `None`. `require_write` adds the one MCP-side write
gate on top: the model must be named in `WRITABLE_MODELS`; per-user
authorization stays with the admin's own permission checks.
`validate_lookup_path` guards `search_objects` filters: the root
must be a real concrete field (unknown is an error, never a silent no-op)
and no segment may name a redacted field, which closes the
filter-as-oracle side channel (`password__startswith="a"` would leak a
redacted value one character at a time).

**`django_admin_fastmcp/tools/__init__.py`**

```python
"""Shared resolution for every tool (SPEC.md section 5).

Each tool resolves its target once, through `resolve`. Denies, in order: an
inactive user, a non-staff user, a hidden model, an unregistered model, and a
ModelAdmin that refuses the action. Every failure path denies. No branch
allows a call because a lookup returned None.
"""

from typing import Literal

from django.apps import apps
from django.contrib.admin import ModelAdmin
from django.db.models import Model
from django.http import HttpRequest

from django_admin_fastmcp import exposure
from django_admin_fastmcp.errors import ToolError, denied
from django_admin_fastmcp.request import admin_request

Action = Literal["view", "add", "change", "delete"]


def resolve(user, label: str, action: Action) -> tuple[type[Model], ModelAdmin, HttpRequest]:
    """Return the model, its ModelAdmin, and a synthetic request, or raise."""
    if not (user.is_active and user.is_staff):
        raise denied("not a staff user")
    if not exposure.is_exposed(label):
        raise ToolError(f"model {label} is not exposed")
    model = apps.get_model(label)  # is_exposed already resolved it
    model_admin = exposure.admin_site()._registry.get(model)
    if model_admin is None:
        raise ToolError(f"model {label} is not registered in the admin")
    request = admin_request(user, model_admin, method="GET" if action == "view" else "POST")
    if not getattr(model_admin, f"has_{action}_permission")(request):
        raise denied(f"no {action} permission on {label}")
    return model, model_admin, request


def require_write(label: str) -> None:
    """The deployment-level write gate (SPEC.md section 9).

    WRITABLE_MODELS is the only MCP-side gate. Per-user authorization is
    entirely the admin's: `resolve` asks has_add/change/delete_permission
    right after this, so a user without the model permission is refused
    there. Leave a model out of WRITABLE_MODELS to ban all writes to it, an
    event log for example, whatever any user may do in the admin UI.
    """
    if not exposure.is_writable(label):
        raise denied(f"model {label} is not in WRITABLE_MODELS")


def validate_lookup_path(model: type[Model], lookup: str) -> None:
    """A filter or ordering key must start at a real concrete field, and no
    segment may name a redacted field. Unknown is an error, never a no-op."""
    from django_admin_fastmcp.serialize import concrete_fields

    segments = lookup.removeprefix("-").split("__")
    field_names = {f.name for f in concrete_fields(model)}
    if segments[0] not in field_names:
        raise ToolError(f"unknown field {segments[0]!r} on {exposure.label_of(model)}")
    for segment in segments:
        if exposure.is_redacted(segment):
            raise denied(f"cannot filter or order by redacted field {segment!r}")
```

### 16. Introspection: list_models, describe_model

`list_models` is where per-user variation lives: the tool catalogue is
static, but this answer is filtered by `has_view_permission` and reports the
four permission flags per model. `describe_model` gives an agent enough to
act without guessing: fields with types and requiredness, the admin's list
configuration, readonly fields, inlines, reverse relation names, and the
actions available to this caller (from `get_actions(request)`, which already
applies each action's `allowed_permissions`). Tool docstrings are the
agent-facing documentation, written for a reader who has never seen the
project.

**`django_admin_fastmcp/tools/introspect.py`**

```python
"""list_models, describe_model (SPEC.md section 4)."""

from typing import Any

from django.db.models import Field
from django.urls import NoReverseMatch, reverse

from django_admin_fastmcp import exposure
from django_admin_fastmcp.auth import current_user
from django_admin_fastmcp.request import admin_request
from django_admin_fastmcp.serialize import concrete_fields
from django_admin_fastmcp.tools import resolve


def _changelist_url(model) -> str | None:
    opts = model._meta
    try:
        return reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")
    except NoReverseMatch:
        return None


def list_models() -> list[dict[str, Any]]:
    """List every model you may read over this connection.

    Returns one entry per model with its "app_label.ModelName" label (the
    `model` argument every other tool takes), verbose names, your view, add,
    change, and delete permission flags, and a link into the real admin.
    Models you cannot view are omitted entirely.
    """
    user = current_user()
    result = []
    for model, model_admin in exposure.exposed_models():
        request = admin_request(user, model_admin)
        if not model_admin.has_view_permission(request):
            continue
        opts = model._meta
        result.append(
            {
                "model": exposure.label_of(model),
                "verbose_name": str(opts.verbose_name),
                "verbose_name_plural": str(opts.verbose_name_plural),
                "permissions": {
                    "view": True,
                    "add": model_admin.has_add_permission(request),
                    "change": model_admin.has_change_permission(request),
                    "delete": model_admin.has_delete_permission(request),
                },
                "admin_url": _changelist_url(model),
            }
        )
    return sorted(result, key=lambda entry: entry["model"])


def _field_info(field: Field) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": field.name,
        "type": field.get_internal_type(),
        "required": not field.blank and field.editable and not field.has_default(),
        "editable": field.editable,
    }
    if field.help_text:
        info["help_text"] = str(field.help_text)
    if field.choices:
        info["choices"] = [[value, str(label)] for value, label in field.flatchoices]
    if field.is_relation and field.related_model is not None:
        info["related_model"] = exposure.label_of(field.related_model)
    return info


def describe_model(model: str) -> dict[str, Any]:
    """Describe one model: fields, admin configuration, and your actions.

    `model` is "app_label.ModelName" as returned by list_models. The answer
    covers field names, types, whether each is required, choices and related
    models, the admin's list display, filters, search fields and ordering,
    which fields are read-only for you, inline models, reverse relation
    names (query those through their own model), and the actions you may run
    with run_action. Refuses models you cannot view.
    """
    user = current_user()
    model_class, model_admin, request = resolve(user, model, "view")

    field_names = exposure.visible_field_names(
        model_admin, [f.name for f in concrete_fields(model_class)]
    )
    fields = [_field_info(f) for f in concrete_fields(model_class) if f.name in field_names]

    def _names(items) -> list[str]:
        return [
            item if isinstance(item, str) else getattr(item, "__name__", str(item))
            for item in items
        ]

    reverse_relations = sorted(
        f.get_accessor_name()
        for f in model_class._meta.get_fields()
        if f.auto_created
        and not f.concrete
        and f.related_model is not None
        and f.get_accessor_name()
    )
    actions = {
        name: str(description)
        for func, name, description in model_admin.get_actions(request).values()
    }
    return {
        "model": exposure.label_of(model_class),
        "fields": fields,
        "list_display": _names(model_admin.get_list_display(request)),
        "list_filter": _names(model_admin.get_list_filter(request)),
        "search_fields": list(model_admin.get_search_fields(request)),
        "ordering": list(model_admin.get_ordering(request) or ()),
        "readonly_fields": _names(model_admin.get_readonly_fields(request)),
        "inlines": [
            exposure.label_of(inline.model) for inline in model_admin.get_inline_instances(request)
        ],
        "reverse_relations": reverse_relations,
        "actions": actions,
    }
```

### 17. Reads

One rule makes row-level security free: every read starts from
`ModelAdmin.get_queryset(request)`, never `Model.objects.all()`. A
`get_queryset` override that hides rows therefore hides them from search,
from `get_object`, and from every write that fetches first. `q` goes
through the admin's own `get_search_results`. Filters and ordering are
validated (section 15) then applied inside try/except so an invalid value
is an error, not an unfiltered result set. `page_size` is capped, not
rejected. History tools read `LogEntry`: `recent_actions` scopes to the
caller unless they are a superuser, same as the admin index.

**`django_admin_fastmcp/tools/read.py`**

```python
"""search_objects, get_object, object_history, recent_actions (SPEC.md section 4).

Every read starts from `ModelAdmin.get_queryset(request)`, never from
`Model.objects.all()`. That single rule is what makes row-level overrides
apply for free.
"""

from typing import Any

from django.contrib.admin.models import LogEntry
from django.core.exceptions import FieldError, ValidationError
from django.db.models import Model

from django_admin_fastmcp import conf, exposure
from django_admin_fastmcp.auth import current_user
from django_admin_fastmcp.errors import ToolError, not_found
from django_admin_fastmcp.serialize import serialize_instance
from django_admin_fastmcp.tools import resolve, validate_lookup_path


def _get_visible(queryset, model: type[Model], pk: str):
    """Fetch one row from an admin queryset. Hidden and missing look the same."""
    label = exposure.label_of(model)
    try:
        return queryset.get(pk=pk)
    except (model.DoesNotExist, ValueError, ValidationError):
        raise not_found(label, pk) from None


def search_objects(
    model: str,
    q: str | None = None,
    filters: dict[str, Any] | None = None,
    order_by: list[str] | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    """Search rows of one model, as the admin changelist would.

    `model` is "app_label.ModelName". `q` runs the admin's own search over
    the model's search_fields. `filters` is a mapping of Django field lookups
    to values, for example {"published": true} or {"author__name": "Ada"}.
    An unknown field or lookup is an error, never a silent no-op. `order_by`
    is a list of field names, prefix "-" for descending. `page_size` is
    capped by the server. Returns {"rows": [...], "total": n, "page": n}.
    Rows you may not see, per the admin's own queryset, are absent.
    """
    user = current_user()
    model_class, model_admin, request = resolve(user, model, "view")
    queryset = model_admin.get_queryset(request)

    if q:
        queryset, may_have_duplicates = model_admin.get_search_results(request, queryset, q)
        if may_have_duplicates:
            queryset = queryset.distinct()
    if filters:
        for key in filters:
            validate_lookup_path(model_class, key)
        try:
            queryset = queryset.filter(**filters)
        except (FieldError, ValueError, ValidationError) as error:
            raise ToolError(f"invalid filters: {error}") from None
    if order_by:
        for key in order_by:
            validate_lookup_path(model_class, key)
        try:
            queryset = queryset.order_by(*order_by)
        except FieldError as error:
            raise ToolError(f"invalid order_by: {error}") from None

    page = max(page, 1)
    page_size = max(1, min(page_size, conf.get("MAX_PAGE_SIZE")))
    total = queryset.count()
    start = (page - 1) * page_size
    rows = [
        serialize_instance(obj, model_admin, request) for obj in queryset[start : start + page_size]
    ]
    return {"rows": rows, "total": total, "page": page, "page_size": page_size}


def get_object(model: str, pk: str) -> dict[str, Any]:
    """Read one object by primary key.

    `model` is "app_label.ModelName", `pk` is the primary key as a string.
    Returns the serialized instance: field values (sensitive fields read
    "[redacted]"), which fields are read-only for you, and a link into the
    real admin. Refuses objects the admin would not show you.
    """
    user = current_user()
    model_class, model_admin, request = resolve(user, model, "view")
    obj = _get_visible(model_admin.get_queryset(request), model_class, pk)
    return serialize_instance(obj, model_admin, request)


def _serialize_log_entry(entry: LogEntry) -> dict[str, Any]:
    return {
        "action_time": entry.action_time.isoformat(),
        "user": str(entry.user),
        "action": entry.get_action_flag_display(),
        "model": (
            f"{entry.content_type.app_label}.{entry.content_type.model}"
            if entry.content_type
            else None
        ),
        "object_id": entry.object_id,
        "object_repr": entry.object_repr,
        "change_message": entry.get_change_message(),
    }


def object_history(model: str, pk: str) -> list[dict[str, Any]]:
    """Read the admin history of one object, newest first.

    Returns the admin LogEntry rows: when, who, what action, and the change
    message. MCP edits appear here too, marked with the client name.
    """
    from django.contrib.contenttypes.models import ContentType

    user = current_user()
    model_class, model_admin, request = resolve(user, model, "view")
    obj = _get_visible(model_admin.get_queryset(request), model_class, pk)
    entries = (
        LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(model_class),
            object_id=str(obj.pk),
        )
        .select_related("user", "content_type")
        .order_by("-action_time")
    )
    return [_serialize_log_entry(entry) for entry in entries]


def recent_actions(limit: int = 25) -> list[dict[str, Any]]:
    """List recent admin actions, newest first.

    Shows your own actions. A superuser sees everyone's. `limit` is capped
    by the server.
    """
    user = current_user()
    limit = max(1, min(limit, conf.get("MAX_PAGE_SIZE")))
    entries = LogEntry.objects.select_related("user", "content_type").order_by("-action_time")
    if not user.is_superuser:
        entries = entries.filter(user=user)
    return [_serialize_log_entry(entry) for entry in entries[:limit]]
```

### 18. Writes

The write path is the admin's own: `get_form` builds the ModelForm,
`save_model` and `save_related` run any overrides, and one `LogEntry` per
mutation records the client name. Everything mutating runs inside
`transaction.atomic()`, so a write that cannot log rolls back (mandatory
audit). `create_object` refuses unknown fields. `update_object` builds the
form with only the submitted fields, ignores readonly and non-editable
fields (reported back as `ignored_readonly_fields`), and re-checks
object-level `has_change_permission(request, obj)` on the fetched instance,
which is where object-level overrides live. `delete_object` previews by
default with the admin's exact `get_deleted_objects` cascade and refuses
when the cascade needs permissions the caller lacks. Reviewer note: the
`_log` helper has a Django 5.0 fallback (`log_action`) that this
environment (Django 6.1) cannot exercise; CI across the version matrix is
the open item that will.

**`django_admin_fastmcp/tools/write.py`**

```python
"""create_object, update_object, delete_object (SPEC.md section 4).

Writes go through the admin's own ModelForm and save_model, then record a
LogEntry. Every write tool runs inside `transaction.atomic()`. A write that
cannot record a LogEntry rolls back (SPEC.md section 9).
"""

from typing import Any

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.db import transaction

from django_admin_fastmcp.auth import client_name, current_user
from django_admin_fastmcp.errors import ToolError, denied
from django_admin_fastmcp.request import collected_messages
from django_admin_fastmcp.serialize import serialize_instance
from django_admin_fastmcp.tools import require_write, resolve
from django_admin_fastmcp.tools.read import _get_visible


def _log(request, obj, action_flag: int, change_message: str) -> None:
    """One LogEntry per mutation, attributed to the grant's user."""
    if hasattr(LogEntry.objects, "log_actions"):
        LogEntry.objects.log_actions(
            request.user.pk, [obj], action_flag, change_message, single_object=True
        )
    else:  # Django 5.0
        from django.contrib.contenttypes.models import ContentType

        LogEntry.objects.log_action(
            user_id=request.user.pk,
            content_type_id=ContentType.objects.get_for_model(type(obj)).pk,
            object_id=obj.pk,
            object_repr=str(obj),
            action_flag=action_flag,
            change_message=change_message,
        )


def _via_mcp(text: str) -> str:
    return f"{text} Via MCP (client: {client_name()})."


def _form_errors(form) -> ToolError:
    errors = {
        field: [e["message"] for e in field_errors]
        for field, field_errors in form.errors.get_json_data().items()
    }
    return ToolError(f"validation failed: {errors}")


def _check_field_names(form_class, data: dict[str, Any], label: str) -> None:
    unknown = set(data) - set(form_class.base_fields)
    if unknown:
        raise ToolError(f"unknown fields for {label}: {sorted(unknown)}")


def create_object(model: str, data: dict[str, Any]) -> dict[str, Any]:
    """Create one object, exactly as the admin add form would.

    `model` is "app_label.ModelName". `data` maps field names to values:
    scalars by value, foreign keys by primary key, many-to-many by a list of
    primary keys (use the autocomplete tool to find them). Validation is the
    admin form's own. Refuses when you lack add permission, when the model
    is not writable on this server, or when your grant is read-only.
    Returns the created object.
    """
    user = current_user()
    require_write(model)
    _, model_admin, request = resolve(user, model, "add")

    form_class = model_admin.get_form(request)
    _check_field_names(form_class, data, model)
    form = form_class(data=data)
    if not form.is_valid():
        raise _form_errors(form)
    with transaction.atomic():
        obj = form.save(commit=False)
        model_admin.save_model(request, obj, form, change=False)
        model_admin.save_related(request, form, formsets=[], change=False)
        _log(request, obj, ADDITION, _via_mcp("Added."))
    result = serialize_instance(obj, model_admin, request)
    result["messages"] = collected_messages(request)
    return result


def update_object(model: str, pk: str, data: dict[str, Any]) -> dict[str, Any]:
    """Partially update one object, as the admin change form would.

    Only the submitted fields change. Fields that are read-only for you are
    ignored, not written. Values follow the same shape as create_object.
    Refuses when you lack change permission on the model or on this exact
    object, when the object is hidden from you, when the model is not
    writable on this server, or when your grant is read-only. Returns the
    updated object.
    """
    user = current_user()
    require_write(model)
    model_class, model_admin, request = resolve(user, model, "change")
    obj = _get_visible(model_admin.get_queryset(request), model_class, pk)
    if not model_admin.has_change_permission(request, obj):
        raise denied(f"no change permission on this {model} object")

    # Readonly covers get_readonly_fields plus non-editable model fields.
    # Submitting one is ignored, not written and not an error (SPEC.md
    # section 11). Anything else outside the admin form is unknown: an error.
    from django_admin_fastmcp.serialize import concrete_fields

    readonly = set(model_admin.get_readonly_fields(request, obj)) | {
        f.name for f in concrete_fields(model_class) if not f.editable
    }
    full_form_class = model_admin.get_form(request, obj, change=True)
    unknown = set(data) - set(full_form_class.base_fields) - readonly
    if unknown:
        raise ToolError(f"unknown fields for {model}: {sorted(unknown)}")
    fields = [name for name in data if name not in readonly]
    if fields:
        form_class = model_admin.get_form(request, obj, change=True, fields=fields)
        form = form_class(data=data, instance=obj)
        if not form.is_valid():
            raise _form_errors(form)
        with transaction.atomic():
            obj = form.save(commit=False)
            model_admin.save_model(request, obj, form, change=True)
            model_admin.save_related(request, form, formsets=[], change=True)
            _log(request, obj, CHANGE, _via_mcp(f"Changed {', '.join(fields)}."))
    result = serialize_instance(obj, model_admin, request)
    result["ignored_readonly_fields"] = sorted(set(data) & readonly)
    result["messages"] = collected_messages(request)
    return result


def _cascade_preview(model_admin, obj, request) -> dict[str, Any]:
    deleted_objects, model_count, perms_needed, protected = model_admin.get_deleted_objects(
        [obj], request
    )

    def _flatten(items) -> list:
        return [_flatten(item) if isinstance(item, list) else str(item) for item in items]

    return {
        "to_delete": _flatten(deleted_objects),
        "counts": {str(name): count for name, count in model_count.items()},
        "perms_needed": sorted(str(p) for p in perms_needed),
        "protected": [str(p) for p in protected],
    }


def delete_object(model: str, pk: str, confirm: bool = False) -> dict[str, Any]:
    """Delete one object, with a mandatory preview.

    With confirm=false (the default) nothing changes: the answer is the exact
    cascade the admin would show, everything that would be deleted along with
    this object. Read it, then call again with confirm=true to delete.
    Refuses when you lack delete permission on the model or on this exact
    object, when the cascade needs permissions you do not have, when the
    model is not writable on this server, or when your grant is read-only.
    """
    user = current_user()
    require_write(model)
    model_class, model_admin, request = resolve(user, model, "delete")
    obj = _get_visible(model_admin.get_queryset(request), model_class, pk)
    if not model_admin.has_delete_permission(request, obj):
        raise denied(f"no delete permission on this {model} object")

    preview = _cascade_preview(model_admin, obj, request)
    if not confirm:
        return {"confirm": False, "would_delete": preview}
    if preview["perms_needed"]:
        raise denied(f"cascade needs permissions you lack: {preview['perms_needed']}")
    if preview["protected"]:
        raise ToolError(f"protected relations prevent deletion: {preview['protected']}")
    object_repr = str(obj)
    with transaction.atomic():
        _log(request, obj, DELETION, _via_mcp("Deleted."))
        model_admin.delete_model(request, obj)
    return {
        "confirm": True,
        "deleted": {"model": model, "pk": pk, "str": object_repr},
        "messages": collected_messages(request),
    }
```

### 19. Actions and autocomplete

`run_action` only runs what `get_actions(request)` returns, so the admin's
own action permissions apply unchanged. All selected pks must be visible
through `get_queryset`, otherwise the call fails with a count rather than
silently operating on fewer rows. Like delete, it previews by default. An
action returning an `HttpResponse` (an export view, say) has no MCP
equivalent; the result says so instead of pretending. `autocomplete`
resolves a relation by searching the related model through the related
admin's own queryset and search, with view permission checked on the
related model, so it cannot be used to read a model the caller may not see.

**`django_admin_fastmcp/tools/actions.py`**

```python
"""run_action (SPEC.md section 4).

Only actions from `ModelAdmin.get_actions(request)`, which already applies
each action's `allowed_permissions`. With `confirm=False` return the action
description and the affected count, and change nothing.
"""

from typing import Any

from django.db import transaction
from django.http import HttpResponse

from django_admin_fastmcp import conf
from django_admin_fastmcp.auth import current_user
from django_admin_fastmcp.errors import ToolError, denied
from django_admin_fastmcp.request import collected_messages
from django_admin_fastmcp.tools import require_write, resolve


def run_action(model: str, action: str, pks: list[str], confirm: bool = False) -> dict[str, Any]:
    """Run an admin action over a set of objects, with a mandatory preview.

    `action` must be one of the actions describe_model lists for you; the
    admin's own permission rules decide that list. `pks` selects the objects,
    all of which must be visible to you. With confirm=false (the default)
    nothing runs: the answer is the action's description and the affected
    count. Call again with confirm=true to run it. Refuses unknown actions,
    hidden objects, models not writable on this server, and read-only grants.
    """
    user = current_user()
    require_write(model)
    if len(pks) > conf.get("MAX_PKS"):
        raise ToolError(f"too many pks: {len(pks)} > MAX_PKS={conf.get('MAX_PKS')}")
    _, model_admin, request = resolve(user, model, "change")

    available = model_admin.get_actions(request)
    if action not in available:
        raise denied(f"action {action!r} is not available to you on {model}")
    entry = available[action]
    # Django 6.1+ exposes attributes; older versions hand out plain tuples.
    func = getattr(entry, "func", None) or entry[0]
    description = getattr(entry, "description", None) or entry[2]

    queryset = model_admin.get_queryset(request).filter(pk__in=pks)
    found = queryset.count()
    if found != len(set(pks)):
        raise ToolError(f"only {found} of {len(set(pks))} selected objects are visible to you")

    if not confirm:
        return {
            "confirm": False,
            "action": action,
            "description": str(description),
            "affected": found,
        }

    with transaction.atomic():
        response = func(model_admin, request, queryset)
    result: dict[str, Any] = {
        "confirm": True,
        "action": action,
        "affected": found,
        "messages": collected_messages(request),
    }
    if isinstance(response, HttpResponse):
        result["note"] = (
            "the action returned an HTTP response, which has no MCP equivalent and was discarded"
        )
    return result
```

**`django_admin_fastmcp/tools/lookup.py`**

```python
"""autocomplete (SPEC.md section 4).

Resolve a foreign-key value to a primary key, through the related model's
`ModelAdmin.get_search_results`. Lets the caller set a relation without
guessing identifiers.
"""

from typing import Any

from django.core.exceptions import FieldDoesNotExist

from django_admin_fastmcp import exposure
from django_admin_fastmcp.auth import current_user
from django_admin_fastmcp.errors import ToolError
from django_admin_fastmcp.tools import resolve

RESULT_CAP = 20


def autocomplete(model: str, field: str, q: str) -> list[dict[str, Any]]:
    """Find primary keys for a relation field by searching labels.

    `model` is the model you are editing, `field` is its foreign-key or
    many-to-many field, and `q` searches the related model the same way its
    admin search box would. Returns up to 20 {"pk", "label"} pairs. Use the
    pk in create_object or update_object. Refuses fields that are not
    relations and related models you may not view.
    """
    user = current_user()
    model_class, _model_admin, _request = resolve(user, model, "view")
    try:
        model_field = model_class._meta.get_field(field)
    except FieldDoesNotExist:
        raise ToolError(f"unknown field {field!r} on {model}") from None
    if not model_field.is_relation or model_field.related_model is None:
        raise ToolError(f"field {field!r} on {model} is not a relation")

    related_label = exposure.label_of(model_field.related_model)
    _related_class, related_admin, related_request = resolve(user, related_label, "view")
    queryset = related_admin.get_queryset(related_request)
    queryset, _ = related_admin.get_search_results(related_request, queryset, q)
    return [{"pk": str(obj.pk), "label": str(obj)} for obj in queryset[:RESULT_CAP]]
```

### 20. Assembly and serving

`build_server` registers the catalogue under wire names `admin_<name>`,
minus `DISABLED_TOOLS`, and attaches the auth provider. `with_auth=False`
exists for the in-memory test transport only; the docstring says never to
serve it. The `instructions` string teaches an agent the intended call
order. The management command serves the app under uvicorn with lifespan
on (the FastMCP session manager starts from the lifespan, which many Django
ASGI setups disable, hence the separate process). Path and default port
derive from `MCP_URL`, so the served route, the advertised resource, and
the token audience cannot drift. `BodySizeLimit` enforces the 256 KiB cap
from SPEC section 9 as pure ASGI middleware.

**`django_admin_fastmcp/server.py`**

```python
"""build_server() -> FastMCP (SPEC.md sections 4 and 12).

Eleven generic tools, mounted under the namespace "admin", so wire names are
"admin_list_models" and so on. The tool list is static and does not vary per
user. Sync `def` tools: FastMCP runs them in a worker thread, where Django's
sync ORM is legal (SPEC.md section 6).
"""

from collections.abc import Callable

from fastmcp import FastMCP

from django_admin_fastmcp import conf
from django_admin_fastmcp.auth import build_auth

MAX_BODY_BYTES = 256 * 1024
NAMESPACE = "admin"


def all_tools() -> dict[str, Callable]:
    """The tool catalogue, keyed by the names DISABLED_TOOLS uses."""
    from django_admin_fastmcp.tools import actions, introspect, lookup, read, write

    return {
        "list_models": introspect.list_models,
        "describe_model": introspect.describe_model,
        "search_objects": read.search_objects,
        "get_object": read.get_object,
        "object_history": read.object_history,
        "recent_actions": read.recent_actions,
        "create_object": write.create_object,
        "update_object": write.update_object,
        "delete_object": write.delete_object,
        "run_action": actions.run_action,
        "autocomplete": lookup.autocomplete,
    }


def build_server(*, with_auth: bool = True) -> FastMCP:
    """Assemble the FastMCP server.

    `with_auth=False` skips the bearer-token layer, for the in-memory test
    transport, which carries no HTTP headers. Never serve it over a network.
    """
    server = FastMCP(
        name=conf.get("SERVER_NAME"),
        auth=build_auth() if with_auth else None,
        instructions=(
            "The Django admin of this project, as tools. Start with "
            "admin_list_models to see what you may touch, then "
            "admin_describe_model before reading or writing a model. "
            "Destructive tools preview first: call them with confirm=false, "
            "read the answer, then confirm."
        ),
    )
    disabled = set(conf.get("DISABLED_TOOLS"))
    for name, fn in all_tools().items():
        if name not in disabled:
            server.tool(fn, name=f"{NAMESPACE}_{name}")
    return server


class BodySizeLimit:
    """Pure ASGI middleware: refuse request bodies over MAX_BODY_BYTES."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            length = headers.get(b"content-length")
            if length is not None and int(length) > MAX_BODY_BYTES:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"request body over 256 KiB",
                    }
                )
                return
        await self.app(scope, receive, send)
```

**`django_admin_fastmcp/management/commands/admin_mcp_serve.py`**

```python
"""Run the FastMCP app under its own uvicorn with lifespan enabled (SPEC.md section 12).

Phase 1: a separate process next to the Django project. Nothing about the
host application's serving configuration changes. `stateless_http=True`, so
any instance behind a load balancer can serve any request.

The served path comes from ADMIN_FASTMCP["MCP_URL"], which defaults to
"/admin/mcp". One setting drives the path, the discovery metadata, and the
audience the verifier expects, so the three cannot drift apart.
"""

from urllib.parse import urlsplit

from django.core.management.base import BaseCommand

from django_admin_fastmcp import conf


class Command(BaseCommand):
    help = "Serve the admin MCP server over streamable HTTP."

    def add_arguments(self, parser):
        parser.add_argument(
            "--host",
            default="127.0.0.1",
            help="Address to bind. Defaults to loopback: put a reverse proxy in front.",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=None,
            help="Port to bind. Defaults to the port in MCP_URL, or 8765.",
        )

    def handle(self, *args, **options):
        import uvicorn

        from django_admin_fastmcp.server import BodySizeLimit, build_server

        path = conf.mcp_path()
        host = options["host"]
        port = options["port"] or urlsplit(conf.get("MCP_URL")).port or 8765

        app = build_server().http_app(stateless_http=True, path=path)
        self.stdout.write(
            f"Serving {conf.get('SERVER_NAME')} on http://{host}:{port}{path}\n"
            f"Clients must reach it as {conf.get('MCP_URL')}"
        )
        uvicorn.run(BodySizeLimit(app), host=host, port=port, lifespan="on")
```

### 21. The audit admin

Grants and clients keep admin changelists, but read-only: who authorized
which client, with which scopes, last used when. Nothing is created by
hand; the OAuth flow is the only mint. Revocation is an action, available
with view permission because non-superusers only ever see their own grants
(`get_queryset` scopes them). `mcp_expose = False` here is belt and
suspenders on top of the built-in denylist in exposure.

**`django_admin_fastmcp/admin.py`**

```python
"""Read-only audit changelists for McpClient and McpToken (SPEC.md section 8).

Who authorized which client, with which scopes, last used when. Revocation is
an admin action. Nothing is created by hand: tokens are issued only by the
OAuth flow. Staff see their own grants, superusers see all.
"""

from django.contrib import admin

from django_admin_fastmcp.models import McpClient, McpToken


class ReadOnlyAdmin(admin.ModelAdmin):
    mcp_expose = False  # belt and suspenders on top of the built-in denylist

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(McpClient)
class McpClientAdmin(ReadOnlyAdmin):
    list_display = ("name", "client_id", "created_at")
    search_fields = ("name", "client_id")


@admin.register(McpToken)
class McpTokenAdmin(ReadOnlyAdmin):
    list_display = (
        "client",
        "user",
        "scope_list",
        "access_expires_at",
        "refresh_expires_at",
        "revoked_at",
        "last_used_at",
    )
    list_filter = ("revoked_at",)
    actions = ("revoke_grants",)

    @admin.display(description="scopes")
    def scope_list(self, obj):
        return ", ".join(obj.scopes)

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related("user", "client")
        if request.user.is_superuser:
            return queryset
        return queryset.filter(user=request.user)

    @admin.action(description="Revoke selected grants", permissions=["view"])
    def revoke_grants(self, request, queryset):
        count = 0
        for grant in queryset:
            grant.revoke()
            count += 1
        self.message_user(request, f"Revoked {count} grant(s).")
```

## Part IV: Proof

### 22. The demo project

A real Django project, not settings-only, so `runserver`, `migrate`, and
`admin_mcp_serve` all work against it. The demo app exists to make the
permission matrix testable: every ModelAdmin shape SPEC section 11 names
has a fixture here. `Book` carries the interesting overrides: hidden rows
leave the queryset for non-superusers, locked rows refuse changes at object
level, `created_at` is readonly, `publish_books` requires change
permission, and `save_model` calls `message_user` so the message-sink path
is exercised. `ApiCredential` names its password in `mcp_fields` on
purpose, to prove the allowlist cannot defeat redaction. `ApiCredential` is
also deliberately absent from `WRITABLE_MODELS`, so the matrix has an
exposed, readable model that refuses writes whatever the grant says.

**`tests/project/settings.py`**

```python
"""Settings for the test project (SPEC.md section 2.4).

A real Django project, not settings-only. The `demo` app exists to make the
permission matrix possible (SPEC.md section 11).
"""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

# Make the `demo` app importable both from `manage.py` (cwd inside the
# project) and from pytest (rootdir at the repository top).
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

SECRET_KEY = "test-only-not-a-secret"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_admin_fastmcp",
    "demo",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": PROJECT_DIR / "db.sqlite3",
    },
}

STATIC_URL = "static/"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

ADMIN_FASTMCP = {
    "SERVER_NAME": "demo-admin",
    "EXCLUDE_MODELS": ("auth.Permission", "auth.Group"),
    # ApiCredential stays out on purpose: the matrix needs an exposed,
    # readable model that refuses writes whatever the grant says.
    "WRITABLE_MODELS": ("demo.Book", "demo.Author"),
    "DISABLED_TOOLS": (),
}
```

**`tests/project/demo/models.py`**

```python
"""Models built to exercise the permission matrix (SPEC.md section 11)."""

from django.db import models


class Author(models.Model):
    """Registered with a plain ModelAdmin: search_fields only, no overrides."""

    name = models.CharField(max_length=100)
    bio = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Book(models.Model):
    """The workhorse: hidden rows, object-level locks, readonly fields,
    a gated action, and a save_model override that calls message_user."""

    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
    published = models.BooleanField(default=False)
    hidden = models.BooleanField(default=False, help_text="Hidden rows leave the queryset.")
    locked = models.BooleanField(default=False, help_text="Locked rows refuse changes.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


class ApiCredential(models.Model):
    """Password and api_key exercise redaction. Its admin sets mcp_fields
    including the password, to prove the allowlist does not defeat redaction."""

    name = models.CharField(max_length=100)
    password = models.CharField(max_length=128)
    api_key = models.CharField(max_length=128, blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.name


class SecretProject(models.Model):
    """Registered in the admin, opted out of MCP with mcp_expose = False."""

    codename = models.CharField(max_length=100)

    def __str__(self):
        return self.codename


class UnregisteredThing(models.Model):
    """Never registered in the admin, so never exposed over MCP."""

    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name
```

**`tests/project/demo/admin.py`**

```python
"""ModelAdmins built to exercise the permission matrix (SPEC.md section 11)."""

from django.contrib import admin

from .models import ApiCredential, Author, Book, SecretProject


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    """No overrides at all: default Django permissions apply as-is."""

    search_fields = ("name",)


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = ("title", "author", "published")
    list_filter = ("published",)
    search_fields = ("title",)
    readonly_fields = ("created_at",)
    actions = ("publish_books",)

    def get_queryset(self, request):
        """Row-level scoping: hidden books do not exist for non-superusers."""
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        return queryset.filter(hidden=False)

    def has_change_permission(self, request, obj=None):
        """Object-level override: locked books refuse changes."""
        if obj is not None and obj.locked:
            return False
        return super().has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        self.message_user(request, f"Saved {obj.title!r} through BookAdmin.")

    @admin.action(description="Publish selected books", permissions=["change"])
    def publish_books(self, request, queryset):
        updated = queryset.update(published=True)
        self.message_user(request, f"Published {updated} book(s).")


@admin.register(ApiCredential)
class ApiCredentialAdmin(admin.ModelAdmin):
    # The allowlist names the password on purpose: redaction must win.
    mcp_fields = ("name", "password", "api_key")


@admin.register(SecretProject)
class SecretProjectAdmin(admin.ModelAdmin):
    mcp_expose = False
```

`manage.py` and `demo/apps.py` are standard boilerplate (the settings
module inserts the project directory into `sys.path`, so `demo` imports the
same way under pytest and under `manage.py`).

### 23. Shared fixtures

Four users ladder the privilege levels the matrix needs: superuser, viewer
(view on two models), editor (full demo permissions), and book_editor
(change on Book only, for the cross-model leak test). `grant` re-fetches
the user because Django caches permissions per instance. The three books
cover the plain, hidden, and locked cases.

**`tests/conftest.py`**

```python
"""Shared fixtures: users at every privilege level, and demo data."""

import pytest
from demo.models import ApiCredential, Author, Book
from django.contrib.auth.models import Permission, User


def grant(user: User, *perms: str) -> User:
    """Grant "app_label.codename" permissions and drop the permission cache."""
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )
    return User.objects.get(pk=user.pk)  # fresh instance, no cached perms


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("root", "root@example.com", "pw")


@pytest.fixture
def viewer(db):
    """Staff, may view Book and Author, nothing else."""
    user = User.objects.create_user("viewer", password="pw", is_staff=True)
    return grant(user, "demo.view_book", "demo.view_author")


@pytest.fixture
def editor(db):
    """Staff, full demo.Book and demo.Author permissions."""
    user = User.objects.create_user("editor", password="pw", is_staff=True)
    return grant(
        user,
        "demo.view_book",
        "demo.add_book",
        "demo.change_book",
        "demo.delete_book",
        "demo.view_author",
        "demo.add_author",
        "demo.change_author",
        "demo.delete_author",
        "demo.view_apicredential",
    )


@pytest.fixture
def book_editor(db):
    """Staff, change on Book only. Cannot change Author."""
    user = User.objects.create_user("book_editor", password="pw", is_staff=True)
    return grant(user, "demo.view_book", "demo.view_author", "demo.change_book")


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ada Lovelace", bio="Analyst.")


@pytest.fixture
def books(db, author):
    return {
        "plain": Book.objects.create(title="Plain", author=author),
        "hidden": Book.objects.create(title="Hidden", author=author, hidden=True),
        "locked": Book.objects.create(title="Locked", author=author, locked=True),
    }


@pytest.fixture
def credential(db):
    return ApiCredential.objects.create(
        name="prod", password="hunter2", api_key="k-123", notes="internal"
    )
```

### 24. The authentication matrix

Two halves. The flow half drives the real OAuth views with Django's test
client: registration persists the client and refuses non-loopback http;
anonymous browsers land on the admin login; non-staff get 403; an
unregistered redirect URI never redirects; a code exchanges exactly once
and a replay revokes the grant it minted; a wrong PKCE verifier issues
nothing; refresh rotates both tokens and the old pair dies; requested
scopes are ignored and every grant carries the `admin` scope. The verifier half
calls `verify_token_sync` directly against every bad-token shape, plus
expired, revoked, deactivated, and de-staffed. The last test proves a
grant never exceeds its user's own permissions.

**`tests/test_auth.py`**

```python
"""Authentication matrix (SPEC.md sections 8 and 11)."""

import base64
import hashlib
import json
import secrets
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from django_admin_fastmcp.auth import DjangoAdminTokenVerifier, impersonate
from django_admin_fastmcp.errors import ToolError
from django_admin_fastmcp.models import McpClient, McpToken

REDIRECT_URI = "http://127.0.0.1:33321/callback"
verifier = DjangoAdminTokenVerifier()


def make_pkce():
    code_verifier = secrets.token_urlsafe(40)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, challenge


def register_client(client) -> str:
    response = client.post(
        reverse("django_admin_fastmcp:register"),
        data=json.dumps({"client_name": "Claude Code", "redirect_uris": [REDIRECT_URI]}),
        content_type="application/json",
    )
    assert response.status_code == 201
    return response.json()["client_id"]


def authorize(client, client_id, challenge, scope="admin:read admin:write"):
    """GET the consent page, then approve. Returns the redirect URL."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "state": "st4te",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    page = client.get(reverse("django_admin_fastmcp:authorize"), params)
    assert page.status_code == 200
    response = client.post(
        reverse("django_admin_fastmcp:authorize"), {**params, "decision": "approve"}
    )
    assert response.status_code == 302
    return response["Location"]


def exchange(client, client_id, code, code_verifier):
    return client.post(
        reverse("django_admin_fastmcp:token"),
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        },
    )


def code_from(location: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["st4te"]
    return query["code"][0]


def full_flow(client, user, scope="admin:read admin:write"):
    """Register, authorize as `user`, exchange. Returns the token JSON."""
    client_id = register_client(client)
    client.force_login(user)
    code_verifier, challenge = make_pkce()
    location = authorize(client, client_id, challenge, scope=scope)
    response = exchange(client, client_id, code_from(location), code_verifier)
    assert response.status_code == 200
    return response.json()


# -- discovery and registration ---------------------------------------------


def test_metadata_names_the_endpoints(client, db):
    data = client.get("/.well-known/oauth-authorization-server").json()
    assert data["authorization_endpoint"].endswith("/admin/mcp/authorize")
    assert data["token_endpoint"].endswith("/admin/mcp/token")
    assert data["registration_endpoint"].endswith("/admin/mcp/register")
    assert data["code_challenge_methods_supported"] == ["S256"]


def test_registration_persists_the_client(client, db):
    client_id = register_client(client)
    stored = McpClient.objects.get(client_id=client_id)
    assert stored.name == "Claude Code"
    assert stored.redirect_uris == [REDIRECT_URI]


def test_registration_refuses_non_loopback_http(client, db):
    response = client.post(
        reverse("django_admin_fastmcp:register"),
        data=json.dumps({"redirect_uris": ["http://evil.example.com/cb"]}),
        content_type="application/json",
    )
    assert response.status_code == 400


# -- the authorize endpoint ---------------------------------------------------


def test_anonymous_browser_is_sent_to_the_admin_login(client, db):
    client_id = register_client(client)
    _, challenge = make_pkce()
    response = client.get(
        reverse("django_admin_fastmcp:authorize"),
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("admin:login"))


def test_non_staff_user_is_refused(client, db):
    client_id = register_client(client)
    user = User.objects.create_user("civilian", password="pw")
    client.force_login(user)
    _, challenge = make_pkce()
    response = client.get(
        reverse("django_admin_fastmcp:authorize"),
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 403


def test_unregistered_redirect_uri_never_redirects(client, superuser):
    client_id = register_client(client)
    client.force_login(superuser)
    _, challenge = make_pkce()
    response = client.get(
        reverse("django_admin_fastmcp:authorize"),
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:9/other",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 400


# -- the token endpoint -------------------------------------------------------


def test_full_flow_issues_a_working_token_pair(client, superuser):
    data = full_flow(client, superuser)
    assert data["token_type"] == "Bearer"
    access = verifier.verify_token_sync(data["access_token"])
    assert access is not None
    assert access.client_id == str(superuser.pk)
    assert access.scopes == ["admin"]


def test_a_code_exchanges_exactly_once_and_reuse_revokes(client, superuser):
    client_id = register_client(client)
    client.force_login(superuser)
    code_verifier, challenge = make_pkce()
    code = code_from(authorize(client, client_id, challenge))
    first = exchange(client, client_id, code, code_verifier)
    assert first.status_code == 200
    second = exchange(client, client_id, code, code_verifier)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"
    # the replay revoked the grant the code minted
    assert verifier.verify_token_sync(first.json()["access_token"]) is None


def test_a_wrong_pkce_verifier_denies(client, superuser):
    client_id = register_client(client)
    client.force_login(superuser)
    _, challenge = make_pkce()
    code = code_from(authorize(client, client_id, challenge))
    response = exchange(client, client_id, code, "wrong-verifier-wrong-verifier-wrong")
    assert response.status_code == 400
    assert McpToken.objects.count() == 0


def test_refresh_rotates_both_tokens(client, superuser):
    data = full_flow(client, superuser)
    response = client.post(
        reverse("django_admin_fastmcp:token"),
        {
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"],
            "client_id": McpClient.objects.get().client_id,
        },
    )
    assert response.status_code == 200
    renewed = response.json()
    assert verifier.verify_token_sync(renewed["access_token"]) is not None
    assert verifier.verify_token_sync(data["access_token"]) is None  # rotated away
    replay = client.post(
        reverse("django_admin_fastmcp:token"),
        {
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"],
            "client_id": McpClient.objects.get().client_id,
        },
    )
    assert replay.status_code == 400


def test_requested_scopes_are_ignored_and_admin_is_granted(client, db):
    """Scopes do not carry authorization: the user's permissions do.

    Whatever the client asks for, the grant carries the single "admin"
    scope, and what it may do is decided per call by the admin's own
    permission checks.
    """
    plain_staff = User.objects.create_user("plain", password="pw", is_staff=True)
    data = full_flow(client, plain_staff, scope="everything admin:write root")
    assert data["scope"] == "admin"


# -- the verifier -------------------------------------------------------------


def issue_grant(user) -> tuple[McpToken, str]:
    mcp_client = McpClient.objects.create(name="t", redirect_uris=[REDIRECT_URI])
    grant_row, access, _refresh = McpToken.issue(user=user, client=mcp_client, scopes=["admin"])
    return grant_row, access


def test_verifier_denies_every_bad_token_shape(superuser):
    grant_row, access = issue_grant(superuser)
    assert verifier.verify_token_sync(access) is not None
    assert verifier.verify_token_sync("garbage") is None
    assert verifier.verify_token_sync("damf_only-two") is None
    prefix = grant_row.access_prefix
    assert verifier.verify_token_sync(f"damf_{prefix}_wrong-secret") is None
    assert verifier.verify_token_sync(f"damf_unknown_{access.split('_')[2]}") is None


def test_verifier_denies_expired_and_revoked(superuser):
    grant_row, access = issue_grant(superuser)
    grant_row.access_expires_at = timezone.now() - timedelta(seconds=1)
    grant_row.save()
    assert verifier.verify_token_sync(access) is None

    grant_row, access = issue_grant(superuser)
    grant_row.revoke()
    assert verifier.verify_token_sync(access) is None


def test_verifier_denies_deactivated_and_destaffed_users(db):
    user = User.objects.create_user("fired", password="pw", is_staff=True)
    _, access = issue_grant(user)
    user.is_active = False
    user.save()
    assert verifier.verify_token_sync(access) is None

    user2 = User.objects.create_user("demoted", password="pw", is_staff=True)
    _, access2 = issue_grant(user2)
    user2.is_staff = False
    user2.save()
    assert verifier.verify_token_sync(access2) is None


def test_a_grant_never_exceeds_its_users_permissions(client, db, author):
    """The whole authorization story after the flow: Django permissions.

    A staff user with no model permissions gets a working token that can
    call tools, and every write is refused by the admin's own checks.
    """
    from django_admin_fastmcp.tools.introspect import list_models
    from django_admin_fastmcp.tools.write import create_object

    nobody = User.objects.create_user("nobody", password="pw", is_staff=True)
    with impersonate(nobody):
        assert list_models() == []
        with pytest.raises(ToolError, match="no add permission"):
            create_object("demo.Book", {"title": "Nope", "author": str(author.pk)})
```

### 25. Discovery and startup-check tests

These two files pin the configuration invariants. Discovery: the served
path, the advertised resource, and the metadata a 401 challenge points at
must all agree, and all derive from `MCP_URL`; the alt-urls test proves the
OAuth prefix is genuinely the project's choice. Checks: each error id fires
on the misconfiguration it exists for, and the demo project itself passes
clean.

**`tests/test_discovery.py`**

```python
"""Discovery consistency (SPEC.md sections 8 and 12).

Three URLs must agree, or the OAuth flow deadlocks:

1. the path the endpoint is served on,
2. the `resource` the protected-resource metadata advertises,
3. the audience the verifier compares each token against.

All three derive from ADMIN_FASTMCP["MCP_URL"]. When they drifted apart, a
client that followed discovery asked for one audience, the verifier expected
another, and every call answered 401 with nothing in the logs to explain it.
"""

from urllib.parse import urlsplit

from django.test import override_settings
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from django_admin_fastmcp import conf
from django_admin_fastmcp.server import build_server

WELL_KNOWN = "/.well-known/oauth-protected-resource"


def served_app():
    return build_server().http_app(stateless_http=True, path=conf.mcp_path())


def route_paths(app) -> list[str]:
    """Every route path in the mounted app, with mount prefixes applied."""
    found: list[str] = []

    def walk(routes, prefix=""):
        for route in routes:
            if isinstance(route, Mount):
                walk(route.routes, prefix + route.path)
            elif isinstance(route, Route):
                found.append(prefix + route.path)

    walk(app.routes)
    return found


def test_the_endpoint_defaults_to_admin_mcp():
    assert conf.mcp_path() == "/admin/mcp"
    assert conf.mcp_origin() == "http://127.0.0.1:8765"
    assert "/admin/mcp" in route_paths(served_app())


def test_metadata_advertises_exactly_the_served_url():
    """The one invariant: advertised resource == MCP_URL, no doubled path."""
    with TestClient(served_app()) as client:
        response = client.get(f"{WELL_KNOWN}{conf.mcp_path()}")
    assert response.status_code == 200
    assert response.json()["resource"].rstrip("/") == conf.get("MCP_URL").rstrip("/")


def test_the_unauthenticated_challenge_points_at_a_route_that_exists():
    """A client with no token follows resource_metadata from the 401 header."""
    with TestClient(served_app()) as client:
        denied = client.post(
            conf.mcp_path(),
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert denied.status_code == 401
        advertised = denied.headers["www-authenticate"].split('resource_metadata="')[1].rstrip('"')
        assert client.get(urlsplit(advertised).path).status_code == 200


@override_settings(
    ADMIN_FASTMCP={
        "SITE_URL": "https://admin.example.com",
        "MCP_URL": "https://admin.example.com/admin/mcp",
    }
)
def test_a_deployed_url_survives_the_same_round_trip():
    assert conf.mcp_path() == "/admin/mcp"
    with TestClient(served_app()) as client:
        response = client.get(f"{WELL_KNOWN}/admin/mcp")
    body = response.json()
    assert body["resource"] == "https://admin.example.com/admin/mcp"
    assert body["authorization_servers"] == ["https://admin.example.com/"]


@override_settings(ADMIN_FASTMCP={"MCP_URL": "https://admin.example.com/hooks/mcp/"})
def test_a_custom_path_is_honored_and_normalized():
    assert conf.mcp_path() == "/hooks/mcp"
    assert "/hooks/mcp" in route_paths(served_app())


def test_the_metadata_follows_the_default_mount(client, db):
    """The endpoints urlconf is mounted at /admin/mcp/ by the test project."""
    body = client.get("/.well-known/oauth-authorization-server").json()
    assert body["authorization_endpoint"].endswith("/admin/mcp/authorize")
    assert body["token_endpoint"].endswith("/admin/mcp/token")


@override_settings(ROOT_URLCONF="tests.alt_urls")
def test_the_prefix_is_the_projects_choice(client, db):
    """Nothing in the package hardcodes "admin/mcp".

    A project that mounts the endpoints under /backoffice/oauth/ gets exactly
    that in the metadata, because every advertised URL comes from reverse().
    """
    body = client.get("/.well-known/oauth-authorization-server").json()
    assert body["authorization_endpoint"].endswith("/backoffice/oauth/authorize")
    assert body["registration_endpoint"].endswith("/backoffice/oauth/register")
    assert body["revocation_endpoint"].endswith("/backoffice/oauth/revoke")
    assert client.get("/backoffice/oauth/authorize").status_code in (302, 400)
```

**`tests/test_checks.py`**

```python
"""Startup validation (SPEC.md section 3).

A misconfigured URL must fail at startup, not at the first client call. Before
these checks existed, a wrong MCP_URL surfaced as a bare 401 with nothing in
the logs to explain it.
"""

from django.core.checks import Error
from django.test import override_settings

from django_admin_fastmcp.checks import check_settings


def ids() -> set[str]:
    return {message.id for message in check_settings(None)}


def test_the_demo_project_is_configured_correctly():
    assert [m for m in check_settings(None) if isinstance(m, Error)] == []


@override_settings(ADMIN_FASTMCP={"MCP_URL": "http://127.0.0.1:8765"})
def test_an_mcp_url_without_a_path_is_an_error():
    """A pathless URL would advertise the whole origin as the protected resource."""
    assert "django_admin_fastmcp.E005" in ids()


@override_settings(ADMIN_FASTMCP={"MCP_URL": "127.0.0.1:8765/admin/mcp"})
def test_a_relative_mcp_url_is_an_error():
    assert "django_admin_fastmcp.E005" in ids()


@override_settings(ADMIN_FASTMCP={"SITE_URL": "example.com"})
def test_a_relative_site_url_is_an_error():
    assert "django_admin_fastmcp.E006" in ids()


@override_settings(ADMIN_FASTMCP={"MODELS": ("demo.NoSuchModel",)})
def test_an_unknown_model_is_an_error():
    assert "django_admin_fastmcp.E002" in ids()


@override_settings(ADMIN_FASTMCP={"DISABLED_TOOLS": ("admin_list_models",)})
def test_a_tool_name_with_the_wire_prefix_is_an_error():
    """DISABLED_TOOLS names catalogue keys, not wire names: "list_models"."""
    assert "django_admin_fastmcp.E003" in ids()


@override_settings(ADMIN_FASTMCP={"WRITABLE_MODELS": ("auth.Group",)})
def test_writable_permission_models_warn():
    assert "django_admin_fastmcp.W001" in ids()
```

### 26. The authorization matrix

The core of SPEC section 11, called straight through the tool functions
under `impersonate`. Highlights: `list_models` differs per user while the
wire catalogue does not; a `get_queryset` override hides rows from search,
get, and update alike; object-level `has_change_permission` allows one
instance and refuses another; change permission on Book does not leak to
Author; every unexposed-model shape is refused with the same message. Two
tests go through a real in-memory MCP client; the transaction=True comment
explains the worker-thread connection issue that requires it.

**`tests/test_permissions.py`**

```python
"""Authorization matrix (SPEC.md sections 5 and 11)."""

import asyncio

import pytest

from django_admin_fastmcp.auth import impersonate
from django_admin_fastmcp.errors import ToolError
from django_admin_fastmcp.server import build_server
from django_admin_fastmcp.tools.actions import run_action
from django_admin_fastmcp.tools.introspect import describe_model, list_models
from django_admin_fastmcp.tools.read import get_object, search_objects
from django_admin_fastmcp.tools.write import create_object, delete_object, update_object


def labels(user) -> set[str]:
    with impersonate(user):
        return {entry["model"] for entry in list_models()}


def test_a_superuser_reaches_every_exposed_model(superuser):
    exposed = labels(superuser)
    assert {"demo.Book", "demo.Author", "demo.ApiCredential", "auth.User"} <= exposed
    assert "demo.SecretProject" not in exposed
    assert "demo.UnregisteredThing" not in exposed
    assert not any(label.startswith("django_admin_fastmcp.") for label in exposed)


def test_list_models_differs_per_user(superuser, viewer):
    assert labels(viewer) == {"demo.Book", "demo.Author"}
    assert labels(viewer) < labels(superuser)


def test_view_only_staff_can_read_but_not_write(viewer, books, author):
    with impersonate(viewer):
        assert search_objects("demo.Book")["total"] == 2  # hidden row absent
        get_object("demo.Book", str(books["plain"].pk))
        with pytest.raises(ToolError, match="no add permission"):
            create_object("demo.Book", {"title": "X", "author": str(author.pk)})
        with pytest.raises(ToolError, match="no change permission"):
            update_object("demo.Book", str(books["plain"].pk), {"title": "X"})
        with pytest.raises(ToolError, match="no delete permission"):
            delete_object("demo.Book", str(books["plain"].pk))


def test_change_on_model_a_does_not_leak_to_model_b(book_editor, books, author):
    with impersonate(book_editor):
        update_object("demo.Book", str(books["plain"].pk), {"title": "Renamed"})
        with pytest.raises(ToolError, match="no change permission"):
            update_object("demo.Author", str(author.pk), {"name": "X"})
    books["plain"].refresh_from_db()
    assert books["plain"].title == "Renamed"


def test_get_queryset_override_hides_rows_everywhere(editor, superuser, books):
    hidden_pk = str(books["hidden"].pk)
    with impersonate(editor):
        rows = search_objects("demo.Book")
        assert all(row["fields"]["title"] != "Hidden" for row in rows["rows"])
        with pytest.raises(ToolError, match="not found"):
            get_object("demo.Book", hidden_pk)
        with pytest.raises(ToolError, match="not found"):
            update_object("demo.Book", hidden_pk, {"title": "X"})
    with impersonate(superuser):  # the override lets superusers through
        assert get_object("demo.Book", hidden_pk)["fields"]["title"] == "Hidden"


def test_object_level_has_change_permission(editor, books):
    with impersonate(editor):
        update_object("demo.Book", str(books["plain"].pk), {"title": "Fine"})
        with pytest.raises(ToolError, match="no change permission on this"):
            update_object("demo.Book", str(books["locked"].pk), {"title": "Nope"})
    books["locked"].refresh_from_db()
    assert books["locked"].title == "Locked"


def test_run_action_refuses_actions_outside_get_actions(viewer, editor, books):
    pks = [str(books["plain"].pk)]
    # no change permission, so the action is filtered out for the viewer
    with impersonate(viewer), pytest.raises(ToolError):
        run_action("demo.Book", "publish_books", pks)
    with impersonate(editor), pytest.raises(ToolError, match="not available"):
        run_action("demo.Book", "no_such_action", pks)


def test_unexposed_models_are_refused(superuser):
    with impersonate(superuser):
        for label in (
            "demo.UnregisteredThing",  # never registered in the admin
            "demo.SecretProject",  # mcp_expose = False
            "auth.Permission",  # EXCLUDE_MODELS
            "django_admin_fastmcp.McpToken",  # built-in denylist
            "no.SuchModel",
        ):
            with pytest.raises(ToolError, match="not exposed"):
                describe_model(label)


def test_default_permissions_apply_without_overrides(viewer, superuser, credential):
    # AuthorAdmin has no overrides; ApiCredential needs its own view permission.
    with impersonate(viewer), pytest.raises(ToolError, match="no view permission"):
        get_object("demo.ApiCredential", str(credential.pk))
    with impersonate(superuser):
        get_object("demo.ApiCredential", str(credential.pk))


@pytest.mark.django_db
def test_the_wire_catalogue_is_static_per_user(superuser, viewer):
    """tools/list does not vary per user; list_models does (tested above)."""
    from fastmcp import Client

    async def tool_names():
        async with Client(build_server(with_auth=False)) as client:
            return {tool.name for tool in await client.list_tools()}

    with impersonate(superuser):
        as_superuser = asyncio.run(tool_names())
    with impersonate(viewer):
        as_viewer = asyncio.run(tool_names())
    assert as_superuser == as_viewer
    assert "admin_list_models" in as_superuser
    assert len(as_superuser) == 11


# transaction=True: the MCP client runs the sync tool in a worker thread,
# whose DB connection cannot see this test's uncommitted transaction.
@pytest.mark.django_db(transaction=True)
def test_calls_work_through_a_real_mcp_client(superuser, books):
    from fastmcp import Client

    async def call():
        async with Client(build_server(with_auth=False)) as client:
            result = await client.call_tool(
                "admin_search_objects", {"model": "demo.Book", "q": "Plain"}
            )
            return result.data

    with impersonate(superuser):
        data = asyncio.run(call())
    assert data["total"] == 1
    assert data["rows"][0]["fields"]["title"] == "Plain"


@pytest.mark.django_db
def test_disabled_tools_vanish_from_the_catalogue(settings, superuser):
    from fastmcp import Client

    settings.ADMIN_FASTMCP = {**settings.ADMIN_FASTMCP, "DISABLED_TOOLS": ("delete_object",)}

    async def tool_names():
        async with Client(build_server(with_auth=False)) as client:
            return {tool.name for tool in await client.list_tools()}

    with impersonate(superuser):
        names = asyncio.run(tool_names())
    assert "admin_delete_object" not in names
    assert len(names) == 10
```

### 27. Exposure and serialization tests

Exposure: the built-in denylist survives even explicit allowlisting, the
MODELS allowlist excludes everything else, wildcards work, unresolvable
labels fail closed. Serialization: redaction beats `mcp_fields`, the pk is
always present as a string (the `model_to_dict` failure both prior-art
packages have), foreign keys carry labels, readonly fields are flagged.

**`tests/test_exposure.py`**

```python
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
```

**`tests/test_serialize.py`**

```python
"""Serialization matrix (SPEC.md sections 7 and 11)."""

from django_admin_fastmcp.auth import impersonate
from django_admin_fastmcp.tools.read import get_object


def test_redaction_wins_even_over_an_explicit_mcp_fields_entry(superuser, credential):
    """ApiCredentialAdmin.mcp_fields names the password on purpose."""
    with impersonate(superuser):
        data = get_object("demo.ApiCredential", str(credential.pk))
    assert data["fields"]["password"] == "[redacted]"
    assert data["fields"]["api_key"] == "[redacted]"
    assert data["fields"]["name"] == "prod"
    assert "notes" not in data["fields"]  # outside mcp_fields


def test_the_primary_key_is_always_present_as_a_string(superuser, books):
    """The model_to_dict failure both reference packages have."""
    with impersonate(superuser):
        data = get_object("demo.Book", str(books["plain"].pk))
    assert data["pk"] == str(books["plain"].pk)
    assert isinstance(data["pk"], str)
    assert data["fields"]["id"] == books["plain"].pk


def test_foreign_keys_carry_a_label_not_just_a_pk(superuser, books, author):
    with impersonate(superuser):
        data = get_object("demo.Book", str(books["plain"].pk))
    assert data["fields"]["author"] == {"pk": str(author.pk), "label": "Ada Lovelace"}


def test_readonly_fields_are_flagged_and_admin_url_present(superuser, books):
    with impersonate(superuser):
        data = get_object("demo.Book", str(books["plain"].pk))
    assert "created_at" in data["readonly_fields"]
    assert data["admin_url"] == f"/admin/demo/book/{books['plain'].pk}/change/"
    # dates serialize as ISO 8601 strings
    assert "T" in data["fields"]["created_at"]
```

### 28. Write, action, and smoke tests

Writes: one LogEntry per successful write with the right user, flag, and
client name; a failed write leaves no row and no log; unknown fields are
errors; submitted readonly fields are ignored and reported; the delete
preview is exact and changes nothing; `WRITABLE_MODELS` refuses even a
superuser; unknown filters error instead of returning everything;
`page_size` caps. Actions: preview by default, messages surface, hidden pks
are unreachable, `MAX_PKS` enforced, `WRITABLE_MODELS` refused for actions too. The smoke
test just proves the package imports and is installed.

**`tests/test_writes.py`**

```python
"""Write and data-handling matrix (SPEC.md section 11)."""

import pytest
from demo.models import Book
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry

from django_admin_fastmcp.auth import impersonate
from django_admin_fastmcp.errors import ToolError
from django_admin_fastmcp.tools.read import search_objects
from django_admin_fastmcp.tools.write import create_object, delete_object, update_object


def test_create_goes_through_the_admin_and_logs_once(editor, author):
    with impersonate(editor):
        result = create_object(
            "demo.Book", {"title": "New", "author": str(author.pk), "published": True}
        )
    book = Book.objects.get(title="New")
    assert book.published is True
    assert result["pk"] == str(book.pk)
    # exactly one LogEntry, right user, right flag, client name in the message
    entry = LogEntry.objects.get(object_id=str(book.pk))
    assert entry.action_flag == ADDITION
    assert entry.user_id == editor.pk
    assert "Via MCP (client: test)" in entry.change_message
    # the save_model override's message_user reaches the caller
    assert any("through BookAdmin" in m for m in result["messages"])


def test_a_failed_write_leaves_no_row_and_no_log_entry(editor):
    with impersonate(editor), pytest.raises(ToolError, match="validation failed"):
        create_object("demo.Book", {"title": "Orphan"})  # author is required
    assert not Book.objects.filter(title="Orphan").exists()
    assert LogEntry.objects.count() == 0


def test_unknown_fields_are_an_error_not_a_no_op(editor, author):
    with impersonate(editor), pytest.raises(ToolError, match="unknown fields"):
        create_object(
            "demo.Book",
            {"title": "X", "author": str(author.pk), "no_such_field": 1},
        )


def test_a_submitted_readonly_field_is_ignored_not_written(editor, books):
    book = books["plain"]
    original_created_at = book.created_at
    with impersonate(editor):
        result = update_object(
            "demo.Book",
            str(book.pk),
            {"title": "Retitled", "created_at": "1999-01-01T00:00:00Z"},
        )
    book.refresh_from_db()
    assert book.title == "Retitled"
    assert book.created_at == original_created_at
    assert result["ignored_readonly_fields"] == ["created_at"]
    entry = LogEntry.objects.get(object_id=str(book.pk), action_flag=CHANGE)
    assert "title" in entry.change_message


def test_writes_refuse_models_outside_writable_models(superuser, credential):
    """ApiCredential is exposed and readable, never writable."""
    with impersonate(superuser), pytest.raises(ToolError, match="WRITABLE_MODELS"):
        update_object("demo.ApiCredential", str(credential.pk), {"name": "x"})


def test_delete_previews_by_default_and_the_cascade_is_exact(editor, author, books):
    with impersonate(editor):
        preview = delete_object("demo.Author", str(author.pk))
    assert preview["confirm"] is False
    assert preview["would_delete"]["counts"] == {"authors": 1, "books": 3}
    assert author.__class__.objects.filter(pk=author.pk).exists()  # nothing changed

    with impersonate(editor):
        result = delete_object("demo.Author", str(author.pk), confirm=True)
    assert result["confirm"] is True
    assert not author.__class__.objects.filter(pk=author.pk).exists()
    assert not Book.objects.exists()  # the cascade ran
    entry = LogEntry.objects.get(action_flag=DELETION, object_id=str(author.pk))
    assert entry.user_id == editor.pk


def test_unknown_filter_keys_error_instead_of_returning_everything(superuser, books):
    with impersonate(superuser):
        with pytest.raises(ToolError, match="unknown field"):
            search_objects("demo.Book", filters={"no_such_field": 1})
        with pytest.raises(ToolError, match="redacted"):
            search_objects("demo.ApiCredential", filters={"password__startswith": "h"})


def test_page_size_is_capped_not_rejected(superuser, books):
    with impersonate(superuser):
        result = search_objects("demo.Book", page_size=99999)
    assert result["page_size"] == 200  # MAX_PAGE_SIZE
    assert result["total"] == 3
```

**`tests/test_actions.py`**

```python
"""Action matrix (SPEC.md sections 4 and 11)."""

import pytest
from demo.models import Book

from django_admin_fastmcp.auth import impersonate
from django_admin_fastmcp.errors import ToolError
from django_admin_fastmcp.tools.actions import run_action


def test_actions_preview_by_default(editor, books):
    pks = [str(books["plain"].pk), str(books["locked"].pk)]
    with impersonate(editor):
        preview = run_action("demo.Book", "publish_books", pks)
    assert preview == {
        "confirm": False,
        "action": "publish_books",
        "description": "Publish selected books",
        "affected": 2,
    }
    assert Book.objects.filter(published=True).count() == 0


def test_confirmed_actions_run_and_report_their_messages(editor, books):
    pks = [str(books["plain"].pk), str(books["locked"].pk)]
    with impersonate(editor):
        result = run_action("demo.Book", "publish_books", pks, confirm=True)
    assert result["affected"] == 2
    assert "Published 2 book(s)." in result["messages"]
    assert Book.objects.filter(published=True).count() == 2


def test_hidden_rows_cannot_be_reached_through_pks(editor, books):
    with impersonate(editor), pytest.raises(ToolError, match="visible"):
        run_action(
            "demo.Book",
            "publish_books",
            [str(books["plain"].pk), str(books["hidden"].pk)],
            confirm=True,
        )
    assert Book.objects.filter(published=True).count() == 0


def test_pks_over_max_pks_are_refused(settings, editor, books):
    settings.ADMIN_FASTMCP = {**settings.ADMIN_FASTMCP, "MAX_PKS": 2}
    with impersonate(editor), pytest.raises(ToolError, match="MAX_PKS"):
        run_action("demo.Book", "publish_books", ["1", "2", "3"])


def test_actions_refuse_models_outside_writable_models(superuser, credential):
    """Actions are writes: WRITABLE_MODELS bans them, whoever calls."""
    with impersonate(superuser), pytest.raises(ToolError, match="WRITABLE_MODELS"):
        run_action("demo.ApiCredential", "delete_selected", [str(credential.pk)])
```

**`tests/test_smoke.py`**

```python
"""Scaffolding smoke test: the package imports and Django is configured."""

import django_admin_fastmcp


def test_version():
    assert django_admin_fastmcp.__version__


def test_app_is_installed(settings):
    assert "django_admin_fastmcp" in settings.INSTALLED_APPS
```

### 29. Mechanical and generated files

`django_admin_fastmcp/__init__.py` holds `__version__ = "0.1.0"` (hatch
reads it). The two migrations are `makemigrations` output for the models in
sections 9 and 22, reviewed for shape (unique prefixes indexed, JSON
defaults) but not reproduced. `uv.lock` is machine-generated.
`.python-version` pins 3.14 for development.

## Verification

- `make check` (ruff format, ruff lint, zuban, pytest): clean. 67 tests
  pass in about 7 seconds.
- End-to-end over real HTTP, twice (before and after the URL-layout
  rework): `runserver` on 8000 plus `admin_mcp_serve` on 8765, then a
  script playing Claude Code: discovery -> dynamic registration -> admin
  login -> consent -> code -> token exchange -> MCP calls with the bearer
  token. Output of the final run:

  ```text
  scope: admin:read admin:write
  tools: 11
  created: 4 Written over MCP | ["Saved 'Written over MCP' through BookAdmin."]
  history: root | Added. Via MCP (client: e2e-claude).
  bad token: HTTPStatusError (401)
  ```

  (Run before the write-gate simplification; the scope line reads `admin`
  since then.)

- SPEC section 2.2 spike ran first and passed: a sync tool read the ORM
  over streamable HTTP on Python 3.14 with fastmcp 3.4.6, no
  `SynchronousOnlyOperation`.

Known gaps in verification:

- The Django 5.0 compat paths (`LogEntry.log_action` fallback, action
  tuples) are written but unexercised; the dev environment runs Django 6.1.
  The CI matrix (M3) covers this.
- `test_discovery.py` emits a `StarletteDeprecationWarning` about httpx in
  `starlette.testclient`. Harmless today.

## Coverage

Every file in the repository, and the section that covers it:

| File | Section | Disposition |
|---|---|---|
| `.gitignore` | 1 | described |
| `CHANGELOG.md` | 1 | described |
| `LICENSE` | 1 | described |
| `MANIFEST.in` | 1 | described |
| `Makefile` | 1 | shown |
| `README.md` | 1 | described |
| `SPEC.md` | 1 | described |
| `django_admin_fastmcp/__init__.py` | 29 | described |
| `django_admin_fastmcp/admin.py` | 21 | shown |
| `django_admin_fastmcp/apps.py` | 7 | shown |
| `django_admin_fastmcp/auth.py` | 14 | shown |
| `django_admin_fastmcp/checks.py` | 7 | shown |
| `django_admin_fastmcp/conf.py` | 2 | shown |
| `django_admin_fastmcp/errors.py` | 3 | shown |
| `django_admin_fastmcp/exposure.py` | 4 | shown |
| `django_admin_fastmcp/management/__init__.py` | 20 | described |
| `django_admin_fastmcp/management/commands/__init__.py` | 20 | described |
| `django_admin_fastmcp/management/commands/admin_mcp_serve.py` | 20 | shown |
| `django_admin_fastmcp/migrations/0001_initial.py` | 29 | described |
| `django_admin_fastmcp/migrations/__init__.py` | 29 | described |
| `django_admin_fastmcp/models.py` | 8 | shown |
| `django_admin_fastmcp/oauth.py` | 10 | shown |
| `django_admin_fastmcp/request.py` | 5 | shown |
| `django_admin_fastmcp/serialize.py` | 6 | shown |
| `django_admin_fastmcp/server.py` | 20 | shown |
| `django_admin_fastmcp/templates/django_admin_fastmcp/authorize.html` | 11 | shown |
| `django_admin_fastmcp/tools/__init__.py` | 15 | shown |
| `django_admin_fastmcp/tools/actions.py` | 19 | shown |
| `django_admin_fastmcp/tools/introspect.py` | 16 | shown |
| `django_admin_fastmcp/tools/lookup.py` | 19 | shown |
| `django_admin_fastmcp/tools/read.py` | 17 | shown |
| `django_admin_fastmcp/tools/write.py` | 18 | shown |
| `django_admin_fastmcp/urls.py` | 13 | shown |
| `django_admin_fastmcp/well_known_urls.py` | 13 | shown |
| `pyproject.toml` | 1 | shown |
| `tests/__init__.py` | 28 | described |
| `tests/alt_urls.py` | 13 | shown |
| `tests/conftest.py` | 23 | shown |
| `tests/project/__init__.py` | 22 | described |
| `tests/project/demo/__init__.py` | 22 | described |
| `tests/project/demo/admin.py` | 22 | shown |
| `tests/project/demo/apps.py` | 22 | described |
| `tests/project/demo/migrations/0001_initial.py` | 29 | described |
| `tests/project/demo/migrations/__init__.py` | 29 | described |
| `tests/project/demo/models.py` | 22 | shown |
| `tests/project/manage.py` | 22 | described |
| `tests/project/settings.py` | 22 | shown |
| `tests/project/urls.py` | 13 | shown |
| `tests/test_actions.py` | 28 | shown |
| `tests/test_auth.py` | 24 | shown |
| `tests/test_checks.py` | 25 | shown |
| `tests/test_discovery.py` | 25 | shown |
| `tests/test_exposure.py` | 27 | shown |
| `tests/test_permissions.py` | 26 | shown |
| `tests/test_serialize.py` | 27 | shown |
| `tests/test_smoke.py` | 28 | shown |
| `tests/test_writes.py` | 28 | shown |
| `uv.lock` | 1 | described |


58 files. "Shown" means the full content or a marked excerpt
appears in that section. "Described" means the section explains the file
without reproducing it (prose documents, generated files, empty
`__init__.py` markers).

## Open items / not in this change

- **M3**: the CI matrix (Python 3.11-3.14 x Django 5.0-6.0), PyPI release,
  and the documented `asgi.py` mount recipe for running the MCP server
  inside the main ASGI process.
- **D8 stays open**: whether a superuser grant should be capped. The spec
  defers the answer until the audit log shows real usage.
- **Headless clients**: a CI job cannot click a consent page. Manual token
  minting can return as an opt-in escape hatch if a headless consumer
  appears (recorded in SPEC D3).
- **Expired-row cleanup**: `McpAuthorizationCode` rows and expired grants
  are never purged. Harmless at this scale; a management command or a
  periodic delete would be the fix.
- **`test_smoke.py`** is scaffolding-era and could fold into the matrix
  files.
