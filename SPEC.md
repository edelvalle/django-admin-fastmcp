# django-admin-fastmcp

A reusable Django app that exposes the Django admin as an MCP server, built on FastMCP.

Every tool call runs as the staff user who owns the bearer token. Every tool call asks the
`ModelAdmin` for permission first. A superuser can do everything a superuser can do in the
admin. A staff user can do exactly what that staff user can do in the admin, and nothing
more.

This file is the whole specification. It is written so an engineer or an agent can
bootstrap the project from an empty directory and know when each milestone is done.

Status: implemented through M2 (reads, writes, actions, and the OAuth flow).
Written 2026-08-10, implemented 2026-08-11. M3 (release) remains.

---

## 1. Design principles

Two properties define the package. Every open question resolves back to one of them.

1. **No parallel permission system.** If the admin refuses, the tool refuses.
   Authorization delegates to `ModelAdmin.has_view_permission`, `has_add_permission`,
   `has_change_permission`, `has_delete_permission`, `get_queryset`,
   `get_readonly_fields`, and `get_actions`. Row-level scoping is never written by hand.
   A `get_queryset` override that hides rows hides them from MCP too.
2. **No parallel data surface.** Writes go through the admin's own `ModelForm` and
   `save_model`, then record a `LogEntry`. The admin history page stays truthful.

A third rule follows from reading the prior art (section 10):

3. **Fail closed.** Every unresolved lookup, missing `ModelAdmin`, unknown action, unknown
   field, and unknown tool denies the call. No code path grants access because a lookup
   returned `None`.

### Non-goals

- No per-model generated tools. See section 4.
- No new admin features. The package exposes what the admin already does.
- No customer-facing or multi-tenant MCP server. Those need relationship-based access
  control, which is a different design. This package is for staff and superusers.

---

## 2. Bootstrap

### 2.1 Environment facts, verified 2026-08-10

- `fastmcp` stable is 3.4.6 (2026-08-05). It pins `mcp>=1.24,<2.0`, so it speaks the 2025
  protocol revisions. `stateless_http=True` already removes sticky sessions.
- `fastmcp` 4.0.0b2 (2026-08-07) adds the 2026-07-28 stateless protocol core.
- `mcp` 2.0.0 (2026-07-28) is the reference implementation of that spec revision.

Pin `fastmcp>=3.4,<4`. It is the stable line, and Claude Code and Cursor connect to it
today. Keep every protocol detail inside FastMCP, so the 4.x upgrade is a dependency bump
and not a rewrite. That means no protocol constants, no JSON-RPC envelopes, and no
transport code in this package.

### 2.2 Milestone 0, the spike that can change the plan

Run this before writing any package code.

```bash
mkdir -p /tmp/damf-spike && cd /tmp/damf-spike
uv init --python 3.14
uv add "django>=5.2" "fastmcp>=3.4,<4"
uv run python -c "import fastmcp, mcp; print(fastmcp.__version__, mcp.types.LATEST_PROTOCOL_VERSION)"
```

Then confirm the one behavior the whole design rests on: a **sync** tool function can touch
the Django ORM under the streamable-HTTP transport, without `sync_to_async` and without
raising `SynchronousOnlyOperation`. FastMCP runs a sync tool in a worker thread, so this
should hold. Verify it rather than assume it.

If `fastmcp` does not install on Python 3.14, fall back to 3.13 for the package's own test
matrix. The package supports whatever Django and Python support. It does not need to match
any single consumer.

### 2.3 Commands

```bash
uv sync                                    # install
uv run pytest                              # the permission matrix (section 9)
uv run ruff format . && uv run ruff check --fix .
uv run ty check                            # types
uv run python tests/project/manage.py migrate
uv run python tests/project/manage.py createsuperuser
uv run python tests/project/manage.py admin_mcp_serve
```

### 2.4 Layout

```
django-admin-fastmcp/
    SPEC.md                  # this file
    README.md                # written at M3, derived from this file
    pyproject.toml
    Makefile
    django_admin_fastmcp/
        __init__.py          # __version__
        apps.py              # AppConfig, registers the system checks
        conf.py              # one accessor over the ADMIN_FASTMCP settings dict
        checks.py            # startup validation
        models.py            # McpClient, McpToken
        admin.py             # read-only audit changelists, revocation action
        auth.py              # DjangoAdminTokenVerifier, current_user()
        oauth.py             # the authorization server: metadata, register, authorize, token
        urls.py              # the oauth endpoints, relative: the project picks the prefix
        well_known_urls.py   # the RFC 8414 metadata document, root-mounted
        request.py           # admin_request(user, ...) -> HttpRequest
        exposure.py          # which models are visible, which fields are redacted
        serialize.py         # instance -> dict, from admin metadata
        errors.py            # ToolError and the denial vocabulary
        server.py            # build_server() -> FastMCP
        tools/
            introspect.py    # list_models, describe_model
            read.py          # search_objects, get_object, object_history, recent_actions
            write.py         # create_object, update_object, delete_object
            actions.py       # run_action
            lookup.py        # autocomplete
        management/commands/admin_mcp_serve.py
        migrations/
    tests/
        project/             # a real Django project, not settings-only
            manage.py
            settings.py
            demo/            # models and ModelAdmins built to exercise the matrix
        test_auth.py
        test_discovery.py
        test_permissions.py
        test_exposure.py
        test_serialize.py
        test_writes.py
        test_actions.py
```

The package must not import anything from a consumer project. Consumers contribute
settings only.

### 2.5 `pyproject.toml`

```toml
[project]
name = "django-admin-fastmcp"
version = "0.1.0"
description = "Expose the Django admin as an MCP server, gated by each user's own admin permissions"
requires-python = ">=3.11"
license = "MIT"
dependencies = [
  "django>=5.0",
  "fastmcp>=3.4,<4",
]
classifiers = [
  "Framework :: Django",
  "Framework :: Django :: 5.0",
  "Framework :: Django :: 5.1",
  "Framework :: Django :: 5.2",
  "Framework :: Django :: 6.0",
]

[dependency-groups]
dev = [
  "pytest~=8.4",
  "pytest-django~=4.13",
  "pytest-cov",
  "ruff",
  "ty",
  "uvicorn[standard]",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "tests.project.settings"
addopts = "-vv --ff --maxfail=1"

[tool.ruff]
line-length = 100
target-version = "py311"
```

Test matrix in CI: Python 3.11 to 3.14, Django 5.0 to 6.0, minus the unsupported pairs.

### 2.6 Consumer setup, the target experience

```python
INSTALLED_APPS = [
    ...,
    "django.contrib.admin",
    "django_admin_fastmcp",
]

ADMIN_FASTMCP = {
    "SERVER_NAME": "acme-admin",
    "EXCLUDE_MODELS": ("auth.Permission", "auth.Group"),
    "WRITABLE_MODELS": (),          # empty means no writes at all
    "DISABLED_TOOLS": (),
}
```

```python
# urls.py — the OAuth endpoints ride the same site as the admin
urlpatterns = [
    path("", include("django_admin_fastmcp.well_known_urls")),  # root, fixed by RFC 8414
    path("admin/mcp/", include("django_admin_fastmcp.urls")),   # your prefix
    path("admin/", admin.site.urls),
]
```

```bash
uv run python manage.py migrate django_admin_fastmcp
claude mcp add --transport http acme-admin https://<host>/admin/mcp
# the first call opens the browser: admin login if needed, then a consent page
```

Nothing else. No per-model registration, no mixin, no decorators, no copying tokens.

---

## 3. Settings

Every knob is read through `conf.get(key)`, never from `django.conf.settings` directly.
`conf.get` raises `KeyError` on an unknown key, which catches typos in consumer settings at
startup rather than silently returning `None`.

| Key | Default | Meaning |
|---|---|---|
| `SERVER_NAME` | `"django-admin"` | Name the MCP server advertises. |
| `ADMIN_SITE` | `"django.contrib.admin.site"` | Dotted path to the `AdminSite`. One site per server. |
| `MODELS` | `()` | Allowlist of `"app_label.ModelName"`. When non-empty, nothing else is exposed. |
| `EXCLUDE_MODELS` | `()` | Denylist. Supports `"app_label.*"`. |
| `WRITABLE_MODELS` | `()` | Models that accept writes. Empty means no writes, whatever the token says. |
| `DISABLED_TOOLS` | `()` | Tool names removed from the catalogue entirely. |
| `REDACT_FIELDS` | `("password", "token", "secret", "api_key", "private_key")` | Substring match on field names. Values become `"[redacted]"`. |
| `MAX_PAGE_SIZE` | `200` | Cap on `search_objects` page size. |
| `MAX_PKS` | `1000` | Cap on `pks` per `run_action`. |
| `ACCESS_TOKEN_TTL_MINUTES` | `60` | Access token lifetime. Clients renew with the refresh token. |
| `REFRESH_TOKEN_TTL_DAYS` | `90` | Refresh token lifetime. Re-consent happens this often. |
| `SITE_URL` | `"http://127.0.0.1:8000"` | Public URL of the Django site, the OAuth issuer. |
| `MCP_URL` | `"http://127.0.0.1:8765/admin/mcp"` | Public URL of the MCP endpoint. |

`MCP_URL` is load-bearing. Three things derive from it and must agree, or the OAuth flow
deadlocks:

1. the path the endpoint is served on (`conf.mcp_path()`),
2. the `resource` that the protected-resource metadata advertises,
3. the audience the verifier compares each token's `resource` against.

FastMCP composes the advertised resource as `base_url` plus the mount path, so
`build_auth` passes the origin alone (`conf.mcp_origin()`). Passing the full `MCP_URL`
there advertises the path twice, and then a client that follows discovery asks for an
audience the verifier rejects. `tests/test_discovery.py` locks the invariant.

The path defaults to `/admin/mcp`, next to the OAuth endpoints. Use it without a trailing
slash: `/admin/mcp/` answers a 307 redirect, and not every client follows a redirect on
POST.

`checks.py` validates at startup: every name in `MODELS`, `EXCLUDE_MODELS`, and
`WRITABLE_MODELS` resolves to a real model, every name in `DISABLED_TOOLS` is a real tool
name, `ADMIN_SITE` imports, `MCP_URL` is an absolute http(s) URL with a path, and
`SITE_URL` is an absolute http(s) URL. A bad value is an error, not a warning.

---

## 4. Tool surface

Eleven generic tools. Each takes `model` as `"app_label.ModelName"`. The tool list is
static and does not vary per user.

Per-model tools are rejected. A medium Django project registers 150 to 200 models, which
times four operations is over 700 tools. No client can present that, and no model can
choose from it. All three reference implementations converged on generic tools for the same
reason.

Mount the server under the namespace `admin`, so wire names are `admin_list_models` and so
on. That keeps names unique when a consumer aggregates several MCP servers.

### Read

| Tool | Arguments | Returns |
|---|---|---|
| `list_models` | none | Every exposed model this caller may view. App label, model name, verbose names, the four permission flags for this caller, and the changelist URL. This is where per-user variation lives. |
| `describe_model` | `model` | Fields (name, type, required, choices, `help_text`, related model), `list_display`, `list_filter`, `search_fields`, `ordering`, readonly fields, inline models, reverse relation names, and the actions this caller may run with their descriptions. |
| `search_objects` | `model`, `q=None`, `filters=None`, `order_by=None`, `page=1`, `page_size=25` | Rows plus `total`. `q` goes through `ModelAdmin.get_search_results`. `filters` is a mapping of field lookups, each validated against the model's fields. An unknown lookup is an error, never a silent no-op. |
| `get_object` | `model`, `pk` | One serialized instance. |
| `object_history` | `model`, `pk` | `LogEntry` rows for that object, newest first. |
| `recent_actions` | `limit=25` | `LogEntry` rows. Scoped to the caller unless the caller is a superuser. |

### Write

| Tool | Arguments | Behavior |
|---|---|---|
| `create_object` | `model`, `data` | Validate through `ModelAdmin.get_form(request)`, then `save_model`, `save_related`, `log_addition`. |
| `update_object` | `model`, `pk`, `data` | Partial update. Build the form with only the submitted fields, minus readonly fields. Then `save_model`, `save_related`, `log_change`. |
| `delete_object` | `model`, `pk`, `confirm=False` | With `confirm=False` return the `get_deleted_objects` cascade preview and change nothing. With `confirm=True` call `delete_model` and `log_deletion`. |
| `run_action` | `model`, `action`, `pks`, `confirm=False` | Only actions from `ModelAdmin.get_actions(request)`, which already applies each action's `allowed_permissions`. With `confirm=False` return the action description and the affected count. |
| `autocomplete` | `model`, `field`, `q` | Resolve a foreign-key value to a primary key, through the **related** model's `ModelAdmin.get_search_results`. Lets the caller set a relation without guessing identifiers. |

Every returned row carries `pk` as a string and an `admin_url`, so the model can hand a
person a link into the real admin.

Tool docstrings are the model's only documentation. Write them for a reader who has never
seen this Django project. State what the tool does, what the arguments mean, and what it
refuses.

---

## 5. Permission rules

Each tool resolves its target once, through one function.

```python
def resolve(user, label: str, action: Action) -> tuple[type[Model], ModelAdmin]:
    """Return the model and its ModelAdmin, or raise ToolError.

    Denies, in order: an inactive user, a non-staff user, a hidden model, an
    unregistered model, and a ModelAdmin that refuses `action`. Every failure
    path denies. No branch allows a call because a lookup returned None.
    """
    if not (user.is_active and user.is_staff):
        raise ToolError("not a staff user")
    elif not exposure.is_exposed(label):
        raise ToolError(f"model {label} is not exposed")
    else:
        model = apps.get_model(label)          # LookupError -> ToolError
        model_admin = admin_site()._registry.get(model)
        if model_admin is None:
            raise ToolError(f"model {label} is not registered in the admin")
        else:
            request = admin_request(user, model_admin, action)
            if not getattr(model_admin, f"has_{action}_permission")(request):
                raise ToolError(f"no {action} permission on {label}")
            else:
                return model, model_admin
```

Mapping from tool to check:

| Tool | Check |
|---|---|
| `list_models` | `has_view_permission` per model, used as a filter rather than a denial. |
| `describe_model`, `search_objects`, `get_object`, `object_history` | `has_view_permission` |
| `create_object` | `has_add_permission` |
| `update_object` | `has_change_permission`, then `has_change_permission(request, obj)` on the fetched instance. |
| `delete_object` | `has_delete_permission`, then `has_delete_permission(request, obj)`. |
| `run_action` | membership in `get_actions(request)`, plus `has_change_permission`. |
| `autocomplete` | `has_view_permission` on the related model. |
| `recent_actions` | staff only, same as the admin index. |

Every read starts from `ModelAdmin.get_queryset(request)`. Never from
`Model.objects.all()`. That single rule is what makes row-level overrides apply for free.

Object-level `has_*_permission(request, obj)` is called with the real instance for update
and delete, because that is where object-level overrides live.

Superusers need no special case. They pass every check by construction, which is exactly
the stated requirement.

---

## 6. The synthetic request

`ModelAdmin` methods take an `HttpRequest`. An MCP call has none. Build a minimal one.

```python
def admin_request(user, model_admin=None, action="view") -> HttpRequest:
    """An HttpRequest sufficient for ModelAdmin permission and queryset methods.

    Carries: user, method (GET for reads, POST for writes), a real admin path
    from reverse() so code that inspects request.path resolves sensibly, META
    with the host, empty immutable QueryDicts for GET and POST, an unsaved
    SessionStore, empty COOKIES, and a message sink.

    The message sink matters. Admin actions and save_model overrides routinely
    call self.message_user(request, ...), which raises MessageFailure without
    the messages framework installed on the request. Collected messages are
    returned in the tool result, so an action's own feedback reaches the caller.
    """
```

CSRF is not a concern. The request never passes through the middleware chain, and the
transport authenticates with a bearer token rather than a cookie.

### Sync tools, not async

Tool functions are plain `def`. FastMCP runs a sync tool in a worker thread, so Django's
sync ORM is legal there and `SynchronousOnlyOperation` cannot occur.

This is deliberate. The whole admin API is sync-only. `ModelAdmin.get_form`,
`ModelForm.is_valid`, `save_model`, `get_deleted_objects`, `message_user`, and `LogEntry`
have no async counterpart. `async def` tools would force `sync_to_async` around all of it
and buy nothing.

Every write tool runs inside `transaction.atomic()`.

---

## 7. Exposure and serialization

### Which models

Default: every model in `admin_site._registry`. The package exposes whatever the admin
exposes.

Three filters narrow it:

1. `EXCLUDE_MODELS`, with `"app_label.*"` support.
2. `MODELS`, an allowlist. When set, nothing else is exposed.
3. `mcp_expose = False` on a `ModelAdmin`, which opts that model out from the app's own
   code.

A built-in denylist cannot be overridden: every `django_admin_fastmcp` model,
`sessions.Session`, and `authtoken.Token`. A token or client written through MCP would
let an agent escalate outside the audit trail.

Consumers should also exclude `auth.Permission` and `auth.Group`. An agent that can grant
permissions can leave the permission model behind. The README says so. `checks.py` emits a
warning, not an error, when they are exposed and writable.

### Which fields

Per `ModelAdmin`: `mcp_fields` is an allowlist, `mcp_exclude_fields` is a denylist. Names
match `django-admin-mcp` so the concept transfers for anyone who has seen it.

Independently, a value is redacted when its field name matches `REDACT_FIELDS`. Redaction
returns the string `"[redacted]"` rather than omitting the key, so the model knows the
field exists and does not retry. An explicit `mcp_fields` entry does **not** defeat
redaction. Only editing `REDACT_FIELDS` does.

### How

Never `model_to_dict`. It drops non-editable fields, including the primary key, and it
includes every editable field, including password hashes. Both reference packages make
this mistake.

Serialization walks `model._meta.get_fields()` and produces:

- concrete fields by value, with dates as ISO 8601 strings and `Decimal` as a string,
- the primary key always as a string,
- a foreign key as `{"pk": "...", "label": str(obj)}`, so the caller reads names rather
  than identifiers,
- many-to-many as a list of the same shape, capped and counted,
- fields from `ModelAdmin.get_readonly_fields(request, obj)` flagged as readonly,
- `admin_url` from `reverse("admin:<app>_<model>_change", args=[pk])`.

Reverse relations are not expanded. `describe_model` names them and the caller queries the
other model.

---

## 8. Authentication

OAuth 2.1 authorization-code flow with PKCE, which is what the MCP specification defines
for HTTP transports. The Django project itself is the authorization server. The user
approves the client in the browser, where the admin session cookie already identifies
them. Nobody mints a token by hand, and nobody copies a secret.

Bearer tokens still travel on the wire after the flow completes. What disappears is the
manual step: the flow issues the tokens, short-lived, and the client refreshes
them without user involvement.

### The flow

1. `claude mcp add --transport http acme-admin https://<host>/admin/mcp`. No header.
2. The first call answers 401 with protected-resource metadata (RFC 9728), served by
   FastMCP's `RemoteAuthProvider`, which names the Django site as the authorization
   server.
3. The client reads `/.well-known/oauth-authorization-server` from the Django site
   (RFC 8414) and self-registers through dynamic client registration (RFC 7591). The
   registration becomes an `McpClient` row.
4. The client opens the browser at the authorize endpoint. An anonymous user goes through
   the normal admin login first. A logged-in staff user lands directly on the consent
   page.
5. The consent page shows the client name and states what approval means: the client
   acts with the user's own admin permissions, writes only where the server allows them,
   and every change is logged. Approval is a POST with CSRF protection. It redirects back
   to the client with a single-use authorization code bound to the PKCE challenge.
6. The client exchanges the code and verifier at the token endpoint for an access token
   and a rotating refresh token.

Enforced, not optional: PKCE with S256 only, exact `redirect_uri` match with loopback
addresses allowed for CLI clients, codes that expire in 60 seconds and burn on first use,
and tokens audience-bound to the MCP resource URL (RFC 8707).

### Endpoints

Django views in this package. The consumer decides where they live, in two includes,
because exactly one of these paths is not ours to move:

```python
urlpatterns = [
    # RFC 8414 fixes this at the site root: a client derives the URL from the issuer.
    path("", include("django_admin_fastmcp.well_known_urls")),
    # Your prefix. Match it to the path in MCP_URL.
    path("admin/mcp/", include("django_admin_fastmcp.urls")),
    path("admin/", admin.site.urls),
]
```

| Path | Purpose |
|---|---|
| `/.well-known/oauth-authorization-server` | Authorization server metadata (RFC 8414). Root, fixed. |
| `<prefix>/register` | Dynamic client registration (RFC 7591). |
| `<prefix>/authorize` | Login-gated consent page. |
| `<prefix>/token` | Code and refresh-token exchange. |
| `<prefix>/revoke` | Token revocation (RFC 7009). |

The package hardcodes no prefix. Every URL in the metadata document comes from
`reverse()`, so discovery follows the mount point the project picked, and
`tests/test_discovery.py` proves it with a project that mounts them under
`/backoffice/oauth/`. Two constraints on the choice: the endpoints must sit on the
same site as the admin, because the consent page rides the admin session cookie,
and the MCP endpoint itself is a separate process, so its path comes from `MCP_URL`
rather than from this urlconf.

An earlier draft attached these views to `AdminSite.get_urls` so the prefix would
follow the admin automatically. Rejected: a library that patches the admin site at
import time takes a decision that belongs to the project, and Django's admin
appends a catch-all route, so the patch has to prepend rather than append to stay
reachable at all. An explicit `include()` says where the endpoints are.

The authorize and consent views are the only places the session cookie matters, and they
run in the user's browser, where Django's login redirect and CSRF machinery already work.
The MCP transport itself never sees a cookie. The endpoints live on the Django site even
when the MCP server runs as a separate process (section 12); they share the database.

### Storage

```python
class McpClient(models.Model):
    """A dynamically registered OAuth client (RFC 7591)."""
    client_id = models.CharField(max_length=48, unique=True)
    name = models.CharField(max_length=200)          # e.g. "Claude Code"
    redirect_uris = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class McpToken(models.Model):
    """One grant: user x client, holding the current token pair.

    Only prefixes and salted SHA-256 hashes are stored, so a leaked database
    row cannot be replayed. The access token expires fast and is renewed with
    the refresh token, which rotates on every use. A grant is as privileged as
    its user's admin permissions allow, never more.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="mcp_tokens")
    client = models.ForeignKey(McpClient, on_delete=models.CASCADE)
    scopes = models.JSONField()                       # always ["admin"], see below
    access_prefix = models.CharField(max_length=12, unique=True, db_index=True)
    access_hash = models.CharField(max_length=64)
    refresh_prefix = models.CharField(max_length=12, unique=True, db_index=True)
    refresh_hash = models.CharField(max_length=64)
    salt = models.CharField(max_length=32)
    access_expires_at = models.DateTimeField()
    refresh_expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
```

Secrets are 32 random bytes from `secrets.token_urlsafe`. Because the entropy is high, a
single salted SHA-256 is the right primitive. A slow password hasher would only add
latency to every request. Authorization codes are short-lived rows or signed values;
either way they burn on first use, and a reused code revokes the grant it minted.

Verification of each MCP call runs in a FastMCP `TokenVerifier`:

1. Split the wire token. A malformed value denies.
2. Load by `access_prefix` with `select_related("user")`. A miss denies.
3. Compare hashes with `secrets.compare_digest`. A mismatch denies.
4. Deny if revoked, expired, `not user.is_active`, or `not user.is_staff`.
5. Return an `AccessToken` with `client_id = str(user.pk)` and the grant's scopes.
6. Update `last_used_at` at most once per minute, to keep the write cheap.

### Scopes and the write gate

One scope: `admin`, meaning "act in the admin as this user". Scopes carry no
authorization of their own; requested scopes are ignored and `admin` is always granted.
What a grant may do is decided per call, by exactly two things:

1. `WRITABLE_MODELS` (section 3), the deployment-level gate. A model outside the list
   refuses every write and every action, whoever calls. This is how a deployment bans
   writes to, say, an event-log model, whatever any user may do in the admin UI.
2. The user's own admin permissions, asked through the `ModelAdmin` on every call.

No `write_via_mcp` permission and no per-scope consent: one permission system, Django's.
Whoever may change a model in the admin UI may change it over MCP, when the deployment
lists that model as writable.

### Admin pages

`McpClient` and `McpToken` keep changelists in the admin, but as read-only audit
surfaces: who authorized which client, with which scopes, last used when. Revocation is
an admin action. Nothing is created by hand. Staff see their own grants, superusers see
all.

### Headless clients

A CI job cannot click a consent page. This flow serves interactive clients, which is the
package's stated audience. If a headless consumer appears, manual token minting can
return as an opt-in escape hatch (see D3).

---

## 9. Audit and safety rails

### Audit

Every mutation writes a `LogEntry` through `ModelAdmin.log_addition`, `log_change`, or
`log_deletion`, with `user` set to the grant's user. The `change_message` names the source
and the client, for example `"Changed status. Via MCP (client: Claude Code)."`, so the
admin history page distinguishes an agent edit from a person's edit.

A `django_admin_fastmcp` logger emits one structured record per call: tool name, model
label, user, client name, outcome, and duration. Denials log at `warning`. Secrets are
never logged.

`tracing.py` adds optional Sentry and Logfire instrumentation on the same wrapper,
mirroring djhtmx: a span or transaction per tool call (op `mcp.tool`) tagged with tool,
model, user, client, and outcome, per-tool counters and duration distributions, and
exception capture for outcomes that are bugs rather than denials. Both backends are
optional extras and activate only when installed, initialized, and not turned off by
`ENABLE_SENTRY_TRACING` / `ENABLE_LOGFIRE_TRACING`.

### Rails

An admin MCP server for a superuser is a remote shell over the production database, driven
by a language model. The rails are part of the specification, not an afterthought.

1. **Write allowlist.** `WRITABLE_MODELS` defaults to empty, which means no writes at all
   regardless of token scope. A deployment names the models it accepts writes for.
3. **`DISABLED_TOOLS`** removes tools from the catalogue. A disabled tool is invisible in
   `tools/list` and unknown to `tools/call`.
4. **Confirmation gates.** `delete_object` and `run_action` preview by default and change
   nothing until `confirm=True`. The preview uses the admin's own `get_deleted_objects`, so
   the cascade is exact.
5. **Caps.** `MAX_PAGE_SIZE`, `MAX_PKS`, and a 256 KiB request body cap.
6. **Mandatory audit.** A write that cannot record a `LogEntry` rolls back.

---

## 10. Prior art

Three packages exist. None is a viable dependency. Read all three before implementing.

| Package | Take from it | Do not copy |
|---|---|---|
| [django-admin-mcp-api](https://github.com/MartinCastroAlvarez/django-admin-mcp-api) | Full delegation to `has_*_permission`. A settings namespace with a single accessor that raises on unknown keys. `DISABLED_TOOLS`. The body-size cap and the primary-key length and pattern bounds. Primary keys always serialized as strings. | Session cookie plus CSRF as the only auth, so no remote client can connect. Protocol `2024-11-05` hard-coded. A second package (`django-admin-rest-api`) that it forwards to over HTTP, calling its own process. |
| [django-admin-mcp](https://github.com/7tg/django-admin-mcp) | The synthetic-request pattern. `mcp_fields` and `mcp_exclude_fields`. The token model shape (public prefix, salted hash, expiry, revocation, `last_used_at`). Writes through `ModelAdmin.get_form(request, obj)`. Foreign-key name normalization from `field_id` to `field`. | `check_permission` returns `True` when the `ModelAdmin` is missing, when the user is missing, and for unknown actions. That is fail-open, which inverts the requirement. `serialize_instance` uses `model_to_dict`, so the primary key vanishes and password hashes leak. An opt-in mixin per `ModelAdmin`, which does not meet the goal of exposing whatever the admin exposes. |
| [django-mcp-server](https://github.com/gts360/django-mcp-server) | Nothing directly. Read it for the ASGI and WSGI mounting approach. | Authorization is endpoint-level only, with no admin integration. |

---

## 11. Test plan

FastMCP's in-memory transport (`Client(build_server())`) calls tools through a real MCP
client without HTTP. The suite is a permission matrix, because that is the feature.

`tests/project/demo/` exists to make the matrix possible. It needs a model with a password
field, a `ModelAdmin` with a `get_queryset` override that hides rows, a `ModelAdmin` with
an object-level `has_change_permission`, a `ModelAdmin` with readonly fields, a
`ModelAdmin` with a custom action carrying `allowed_permissions`, a `ModelAdmin` with
`mcp_expose = False`, and a model registered with no overrides at all.

### Authentication

The flow, driven with Django's test client against the OAuth views:

- Dynamic client registration returns a `client_id` and persists the redirect URIs.
- The authorize endpoint redirects an anonymous browser to the admin login.
- A logged-in non-staff user is refused at the consent page.
- Consent approval returns a code, and the code exchanges for a token pair exactly once.
- A reused code denies and revokes the grant it minted.
- A wrong PKCE verifier denies. A `redirect_uri` not registered exactly denies.
- The refresh token renews the access token and rotates itself.
- Requested scopes are ignored: every grant carries exactly the `admin` scope.

The verifier, driven through MCP calls:

- A valid access token succeeds.
- Expired, revoked, unknown prefix, malformed value, and correct prefix with a wrong
  secret all deny.
- A grant whose user was deactivated denies.
- A grant whose user lost `is_staff` denies.
- A read-only grant calling a write tool denies.

### Authorization

- A superuser reaches every exposed model.
- Staff with only `view` on one model: reads succeed, create, update, and delete are
  refused.
- Staff with `change` on model A only cannot change model B.
- A `get_queryset` override that hides rows: `search_objects` and `get_object` cannot see
  them, and `update_object` on a hidden primary key is refused.
- An object-level `has_change_permission(request, obj)`: allowed for one instance, refused
  for another.
- `run_action` refuses an action absent from `get_actions(request)`.
- An unregistered model, an excluded model, an `mcp_expose = False` model, and `McpToken`
  itself are all refused.
- A model registered without permission overrides still honors default Django permissions.
- `list_models` output differs per user, while `tools/list` output does not.

### Data handling

- A readonly field submitted to `update_object` is ignored, not written.
- A password field reads `"[redacted]"` in output, and `mcp_fields` does not defeat that.
- The primary key appears in output. This is the `model_to_dict` failure both references
  have.
- Every successful write leaves exactly one `LogEntry`, with the right user and action
  flag.
- A failed write leaves no `LogEntry` and no row.
- An unknown `filters` key returns an error rather than an unfiltered result set.
- `page_size` above `MAX_PAGE_SIZE` is capped, not rejected silently.
- A `save_model` override that calls `message_user` succeeds, and its message appears in
  the tool result.

---

## 12. Deployment

`build_server().http_app()` returns a mountable Starlette app. The FastMCP streamable-HTTP
session manager starts from the app's lifespan, and many Django ASGI deployments run with
lifespan disabled.

**Phase 1, the separate process.** `manage.py admin_mcp_serve` runs the FastMCP app under
its own uvicorn with lifespan enabled. It serves the path from `MCP_URL` (`/admin/mcp`) on
the port from `MCP_URL`, or 8765 when that URL names none. Nothing about the host
application's serving configuration changes. The ingress routes `/admin/mcp` to that port.
This is what a first consumer points Claude Code at.

**Phase 2, mounted.** Mount at `/admin/mcp` in the project's `asgi.py`, with a small ASGI
dispatcher on the path and a parent lifespan that drives the MCP session manager. Dispatch
on the **exact** path. The OAuth endpoints sit directly below the same prefix
(`/admin/mcp/authorize` and friends) and Django must keep serving those, so a prefix
dispatcher would swallow them. Document both. Ship the command first.

Use `stateless_http=True` in both phases, so any instance behind a load balancer can serve
any request. The ingress must pass the `Authorization` header through.

---

## 13. Milestones and acceptance

**M0. Spike.** Section 2.2. Done when a sync tool reads the ORM over streamable HTTP on
the target Python version. This milestone can fail and change the plan, so it runs first.

**M1. Read-only server.** `conf`, `checks`, `errors`, the models, `oauth`, `urls`, the
audit admin, `auth`, `request`, `exposure`, `serialize`, the six read tools, and
`admin_mcp_serve`.
Done when: Claude Code connects through the browser consent flow with no manual token
step, `list_models` shows only what that user may view, `search_objects` honors a
`get_queryset` override, a password field reads `"[redacted]"`, and the authentication
and read halves of section 11 pass.

**M2. Writes.** `create_object`, `update_object`, `delete_object`, `run_action`,
`autocomplete`, the `LogEntry` path, `WRITABLE_MODELS`, and the confirmation gates.
Done when: the full matrix in section 11 passes, a delete without `confirm` changes
nothing and returns the exact cascade, and every mutation appears in the admin history
page attributed to the right user with the client name in the message.

**M3. Release.** README derived from this file, the CI matrix, a `LICENSE`, and the
`asgi.py` mounting recipe. Done when a fresh project reaches a working MCP connection using
only the README.

---

## 14. Decisions

| # | Decision | Choice | Why |
|---|---|---|---|
| D1 | FastMCP 3 or 4 | 3.x stable now, 4.x when it leaves beta | Clients connect to 3.x today. No protocol code in the package, so the upgrade is a pin bump. |
| D2 | Write access | `WRITABLE_MODELS` is the only MCP-side gate; per-user authorization is the admin's own permissions, nothing else. | One permission system. The deployment bans whole models (an event log); Django permissions decide per user. |
| D3 | Auth method | OAuth 2.1 code + PKCE, with the Django site as authorization server. Consent rides the admin session cookie in the browser. No manual token minting. | Zero-copy setup: `claude mcp add`, log in, approve. The cookie never touches the MCP transport, so remote clients work. Manual minting can return later as an escape hatch for headless clients. |
| D4 | Who may authorize a client | Any staff user, for themselves only. The grant carries the user's own permissions, never more. | Self-serve setup without privilege escalation. |
| D5 | Exposure default | Everything registered, minus the built-in denylist | Matches the goal. Consumers narrow it. |
| D6 | Sync or async tools | Sync `def` | The admin API is sync-only. Async would mean `sync_to_async` around all of it. |
| D7 | Per-model tools | No, eleven generic tools | 700 tools is unusable for any client. |
| D8 | Should a superuser token be capped | Open | An unrestricted superuser token is the stated requirement and also a remote shell. Revisit after M2, once the audit log shows how the tools get used. |
