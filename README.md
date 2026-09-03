# django-admin-fastmcp

A reusable Django app that exposes the Django admin as an MCP server, built on
[FastMCP](https://gofastmcp.com).

Every tool call runs as the staff user who owns the bearer token. Every tool call asks the
`ModelAdmin` for permission first. A superuser can do everything a superuser can do in the
admin. A staff user can do exactly what that staff user can do in the admin, and nothing
more.

`SPEC.md` is the full specification.

## How it works

Three rules define the package:

1. **No parallel permission system.** Authorization delegates to the `ModelAdmin` methods:
   `has_view_permission`, `has_add_permission`, `has_change_permission`,
   `has_delete_permission`, `get_queryset`, `get_readonly_fields`, and `get_actions`.
   A `get_queryset` override that hides rows hides them from MCP too.
2. **No parallel data surface.** Writes go through the admin's own `ModelForm` and
   `save_model`, then record a `LogEntry`. The admin history page stays truthful.
3. **Fail closed.** Every unresolved lookup, missing `ModelAdmin`, unknown action, unknown
   field, and unknown tool denies the call.

## Installation

```bash
uv add django-admin-fastmcp
```

Add the app to your settings:

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
}
```

Mount the OAuth endpoints on the same site as the admin:

```python
# urls.py
urlpatterns = [
    # RFC 8414 fixes this one at the site root.
    path("", include("django_admin_fastmcp.well_known_urls")),
    # This prefix is yours to choose. Match it to the path in MCP_URL.
    path("admin/mcp/", include("django_admin_fastmcp.urls")),
    path("admin/", admin.site.urls),
]
```

The package hardcodes no prefix. Every URL the metadata document advertises comes
from `reverse()`, so a project that mounts the endpoints at `/backoffice/oauth/`
gets that in discovery and clients follow it. Two rules: the well-known document
belongs at the root, because a client derives its URL from the issuer, and the
endpoints should sit on the same site as the admin, because the consent page
rides the admin session cookie.

Apply the migrations:

```bash
python manage.py migrate django_admin_fastmcp
```

Nothing else. No per-model registration, no mixin, no decorators. The server exposes
whatever the admin already exposes.

## Connect a client

Run the server (see [Deployment](#deployment)), then register it:

```bash
# Claude Code
claude mcp add --transport http acme-admin https://<host>/admin/mcp
```

Use the path with no trailing slash. `/admin/mcp/` answers a 307 redirect to
`/admin/mcp`, and not every client follows a redirect on POST.

No token, no header. The first call starts the standard MCP OAuth flow:

1. The client opens your browser at the authorize page on the Django site.
2. Your admin session cookie identifies you. If you are logged out, the normal admin
   login appears first.
3. A consent page shows the client name and what approval means. You approve.
4. The client receives its tokens and connects. It refreshes them by itself.

Any MCP client that speaks streamable HTTP with OAuth works the same way, for example
Cursor or a FastMCP `Client`.

Rules around access:

- Any staff user can authorize a client, for themselves only.
- A grant acts with your own admin permissions, never more. There is no separate
  permission system: whoever may change a model in the admin may change it over MCP,
  when the server lists that model in `WRITABLE_MODELS`.
- Refresh tokens expire after `REFRESH_TOKEN_TTL_DAYS` (default 90), so re-consent
  happens that often. Revocation is an admin action on the grant changelist.

## Tools

Eleven generic tools, mounted under the namespace `admin`, so wire names are
`admin_list_models` and so on. Each takes `model` as `"app_label.ModelName"`. The tool
list is static. What varies per user is what each tool lets that user see and do.

### Read

| Tool | Arguments | Returns |
|---|---|---|
| `list_models` | none | Every exposed model this caller may view, with permission flags. |
| `describe_model` | `model` | Fields, list display, filters, search fields, readonly fields, and available actions. |
| `search_objects` | `model`, `q`, `filters`, `order_by`, `page`, `page_size` | Rows plus `total`. `q` uses the admin's own search. An unknown filter is an error. |
| `get_object` | `model`, `pk` | One serialized instance. |
| `object_history` | `model`, `pk` | Admin log entries for that object, newest first. |
| `recent_actions` | `limit` | Admin log entries, scoped to the caller unless the caller is a superuser. |

### Write

Write tools need the model listed in `WRITABLE_MODELS`; your own admin permissions
decide the rest, per model and per object. A model outside the list refuses every write
and every action, whoever calls. Leave sensitive models out, an event log for example,
and no MCP client can ever write to them.

| Tool | Arguments | Behavior |
|---|---|---|
| `create_object` | `model`, `data` | Validates through the admin form, then saves and logs. |
| `update_object` | `model`, `pk`, `data` | Partial update. Readonly fields are ignored. |
| `delete_object` | `model`, `pk`, `confirm` | Without `confirm`, returns the exact deletion cascade and changes nothing. |
| `run_action` | `model`, `action`, `pks`, `confirm` | Runs an admin action. Without `confirm`, returns a preview. |
| `autocomplete` | `model`, `field`, `q` | Resolves a foreign-key value to a primary key by searching the related model. |

Every returned row carries `pk` as a string and an `admin_url`, so an agent can hand a
person a link into the real admin.

## Settings

All keys live in the `ADMIN_FASTMCP` dict. An unknown key is an error at startup.

| Key | Default | Meaning |
|---|---|---|
| `SERVER_NAME` | `"django-admin"` | Name the MCP server advertises. |
| `ADMIN_SITE` | `"django.contrib.admin.site"` | Dotted path to the `AdminSite`. |
| `MODELS` | `()` | Allowlist of `"app_label.ModelName"`. When non-empty, nothing else is exposed. |
| `EXCLUDE_MODELS` | `()` | Denylist. Supports `"app_label.*"`. |
| `WRITABLE_MODELS` | `()` | Models that accept writes. Empty means no writes, whoever calls. |
| `DISABLED_TOOLS` | `()` | Tool names removed from the catalogue entirely. |
| `REDACT_FIELDS` | `("password", "token", "secret", "api_key", "private_key")` | Substring match on field names. Values read `"[redacted]"`. |
| `MAX_PAGE_SIZE` | `200` | Cap on `search_objects` page size. |
| `MAX_PKS` | `1000` | Cap on `pks` per `run_action`. |
| `ACCESS_TOKEN_TTL_MINUTES` | `60` | Access token lifetime. Clients renew with the refresh token. |
| `REFRESH_TOKEN_TTL_DAYS` | `90` | Refresh token lifetime. Re-consent happens this often. |
| `ENABLE_SENTRY_TRACING` | `True` | Sentry spans and metrics, when `sentry-sdk` is installed and initialized. |
| `ENABLE_LOGFIRE_TRACING` | `True` | Logfire spans and metrics, when `logfire` is installed and configured. |
| `SITE_URL` | `"http://127.0.0.1:8000"` | Public URL of the Django site. It is the OAuth issuer, and the MCP server names it as its authorization server. |
| `MCP_URL` | `"http://127.0.0.1:8765/admin/mcp"` | Public URL of the MCP endpoint. |

Set `SITE_URL` and `MCP_URL` for any real deployment. `MCP_URL` is the single
source of three things that must agree: the path the endpoint is served on, the
`resource` that discovery advertises, and the audience every token is bound to.
Its path defaults to `/admin/mcp`. A startup check refuses a `MCP_URL` with no
path, because then the whole origin would be advertised as the protected
resource.

## Per-ModelAdmin knobs

Set these on a `ModelAdmin` class, no mixin needed:

```python
class InvoiceAdmin(admin.ModelAdmin):
    mcp_expose = False                     # hide this model from MCP entirely
    mcp_fields = ("number", "total")       # allowlist of serialized fields
    mcp_exclude_fields = ("internal_note",)  # denylist of serialized fields
```

`REDACT_FIELDS` wins over `mcp_fields`. Listing a password field explicitly does not
reveal it.

## Safety

An admin MCP server for a superuser is a remote shell over the production database,
driven by a language model. The rails:

- `WRITABLE_MODELS` defaults to empty, so no model accepts writes until the deployment
  names it. Everything else is your ordinary Django permissions, asked through the
  `ModelAdmin` on every call.
- Access tokens are short-lived. Only salted hashes are stored, so a leaked database row
  cannot be replayed.
- `delete_object` and `run_action` preview by default and change nothing until
  `confirm=True`.
- Every mutation records a `LogEntry` attributed to the grant's user, with the client
  name in the change message, for example `"Changed status. Via MCP (client: Claude
  Code)."`. A write that cannot record a `LogEntry` rolls back.
- The package's own models, `sessions.Session`, and `authtoken.Token` are never exposed,
  whatever the settings say.
- Keep `auth.Permission` and `auth.Group` out of `WRITABLE_MODELS`. An agent that can
  grant permissions can escape the permission model.

## Observability

The package instruments every tool call: a Sentry span or transaction named
after the tool (`admin_search_objects`), tagged with the model, the Django
user, the OAuth client, and the outcome (`ok`, `denied`, `error`); a counter
and a duration distribution per tool; and one structured log record per call
on the `django_admin_fastmcp` logger (denials at WARNING). Unexpected
exceptions are captured with full context. Logfire is supported the same way.

Both backends are optional. Nothing here runs unless the library is
installed and initialized:

```bash
uv add "django-admin-fastmcp[sentry]"     # or [logfire], or both
```

### Setting up Sentry

Initialize the SDK in `settings.py`, gated on the DSN, the same way a normal
Django deployment does:

```python
SENTRY_DSN = env.get("SENTRY_DSN")

if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=env.get("SENTRY_ENVIRONMENT", "production"),
        integrations=[DjangoIntegration(), LoggingIntegration()],
        traces_sample_rate=float(env.get("SENTRY_TRACES_SAMPLE_RATE", "0.5")),
        # The spans tag the calling username. Keep this on if you want it.
        send_default_pii=True,
        enable_logs=True,
    )

ADMIN_FASTMCP = {
    ...,
    "ENABLE_SENTRY_TRACING": bool(SENTRY_DSN),
}
```

That single init covers both processes. The web process reports the OAuth
views through `DjangoIntegration` as usual. The `admin_mcp_serve` process
loads the same settings, so the SDK is active there too; the package opens
its own transaction per tool call, so tool traces appear without any ASGI
integration. With `traces_sample_rate` above zero you get per-tool
performance data; with `enable_logs` and the `LoggingIntegration`, the
per-call log records land in Sentry Logs.

What to look at in Sentry:

- **Performance -> transactions** named `admin_<tool>`, op `mcp.tool`:
  who calls what, how often, and how long it takes, filterable by the
  `mcp.model`, `mcp.client`, and `mcp.outcome` tags and by user.
- **Metrics**: `damf.<tool>.ok` / `.denied` / `.error` counters and
  `damf.<tool>.duration_ms` distributions, for dashboards and alerts
  (a spike in `.denied` is an agent probing where it should not).
- **Issues**: real exceptions from tool calls, with the user, client, and
  tool tags attached. Denials are not issues; they are the system working.

For Logfire, call `logfire.configure()` in `settings.py`; the package stays
silent until then, because an unconfigured logfire warns on every span.

Two settings knobs, both default true, turn a backend off without
uninstalling it: `ENABLE_SENTRY_TRACING` and `ENABLE_LOGFIRE_TRACING`.

## Deployment

The MCP endpoint runs as its own process, next to your Django application:

```bash
python manage.py admin_mcp_serve
```

It serves the path from `MCP_URL` on the port in `MCP_URL`, or 8765 when that URL names
no port. `--host` and `--port` override both. Nothing about how you already serve Django
changes. The process is stateless, so any replica can serve any request.

It needs its own process because FastMCP starts the streamable-HTTP session manager from
the ASGI lifespan. A project that serves with lifespan disabled, which is common for
Django, cannot host the MCP app inside its own ASGI application: the session manager
never starts and every call fails. `admin_mcp_serve` runs its own server with lifespan
enabled.

### Behind a reverse proxy

If you already route by path to separate worker pools, this is one more pool. Send the
MCP path to the MCP process and leave every other path where it is. Three rules matter.

**Match the exact path, never the prefix.** The OAuth endpoints sit directly below the
same prefix (`/admin/mcp/authorize`, `token`, `register`, `revoke`), and Django must keep
serving them, because the consent page rides the admin session cookie. A prefix rule
hands them to the MCP process, which knows nothing about them, and no client can ever
authorize. Keep `/.well-known/oauth-authorization-server` on the Django pool too.

**Pass the `Authorization` header through.** The transport authenticates with a bearer
token, so a proxy that strips or rewrites that header denies every call.

**Do not apply an idle-read timeout to this route.** Responses are event streams. A
short read timeout severs a tool call that thinks between writes.

Caddy, where the exact matcher comes first because same-directive routes keep their file
order:

```caddy
# The MCP endpoint. Exact path, so /admin/mcp/authorize stays with Django.
@mcp path /admin/mcp
reverse_proxy @mcp myapp_mcp:8000

# The admin, including the OAuth endpoints under /admin/mcp/.
@admin path /admin /admin/*
reverse_proxy @admin myapp_admin:8000
```

nginx, where `location =` is the exact match and wins over the prefix:

```nginx
location = /admin/mcp { proxy_pass http://myapp_mcp:8000; }
location /admin/      { proxy_pass http://myapp_admin:8000; }
```

With the route in place, `MCP_URL` is your public site URL plus the path, and the port
stays private:

```python
ADMIN_FASTMCP = {
    "SITE_URL": "https://app.example.com",
    "MCP_URL": "https://app.example.com/admin/mcp",
}
```

Both values are public URLs, so derive them from whatever setting already holds your
site's canonical URL rather than writing them twice.

### Under your own server

`admin_mcp_serve` is uvicorn with one setting that matters: `lifespan="on"`. Any ASGI
server works, as long as it runs the lifespan. Expose the app in a module of your own:

```python
# myproject/asgi_mcp.py
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

import django

django.setup()

from django.conf import settings
from django_admin_fastmcp.server import BodySizeLimit, build_server

application = BodySizeLimit(build_server().http_app(stateless_http=True, path="/admin/mcp"))
```

Then serve it the way you serve the rest of your fleet, with lifespan enabled:

```bash
granian --interface asgi --host 0.0.0.0 --port 8000 myproject.asgi_mcp:application
uvicorn --lifespan on --host 0.0.0.0 --port 8000 myproject.asgi_mcp:application
```

Granian's `asginl` interface, and `uvicorn --lifespan off`, both skip the lifespan. A
Django project often chooses one of those for its own app, because Django implements no
lifespan. This app is the opposite: skip the lifespan and the session manager never
starts, so every call answers `RuntimeError: Task group is not initialized`.

### Mounted in your ASGI application

Possible, and rarely worth it. It needs a parent lifespan that drives the FastMCP session
manager, plus a dispatcher on the exact path for the reason above. A separate pool needs
neither, and gives you independent restarts and scaling: an agent that hammers the tools
cannot starve the workers serving people.

## Development

```bash
make install     # bootstrap uv, pin Python, install dependencies
make test        # run the permission matrix
make check       # format, lint, typecheck, and test
make migrate     # migrate the test project
make serve       # run the MCP server against the test project on :8765/admin/mcp
make help        # everything else
```

## License

MIT
