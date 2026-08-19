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
3. A consent page shows the client name and the requested scopes. You approve.
4. The client receives its tokens and connects. It refreshes them by itself.

Any MCP client that speaks streamable HTTP with OAuth works the same way, for example
Cursor or a FastMCP `Client`.

Rules around access:

- Any staff user can authorize a client, for themselves only.
- Grants are read-only by default. The consent page offers the write scope only to users
  who hold the `write_via_mcp` permission, which a superuser grants in the user admin.
- A grant can be less privileged than its user, never more.
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

Write tools need a grant with the `admin:write` scope and a model listed in
`WRITABLE_MODELS`. Both gates apply.

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
| `WRITABLE_MODELS` | `()` | Models that accept writes. Empty means no writes, whatever the token says. |
| `DISABLED_TOOLS` | `()` | Tool names removed from the catalogue entirely. |
| `REDACT_FIELDS` | `("password", "token", "secret", "api_key", "private_key")` | Substring match on field names. Values read `"[redacted]"`. |
| `MAX_PAGE_SIZE` | `200` | Cap on `search_objects` page size. |
| `MAX_PKS` | `1000` | Cap on `pks` per `run_action`. |
| `ACCESS_TOKEN_TTL_MINUTES` | `60` | Access token lifetime. Clients renew with the refresh token. |
| `REFRESH_TOKEN_TTL_DAYS` | `90` | Refresh token lifetime. Re-consent happens this often. |
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

- Grants are read-only by default. The write scope needs the `write_via_mcp` permission,
  which only a superuser grants.
- Access tokens are short-lived. Only salted hashes are stored, so a leaked database row
  cannot be replayed.
- `WRITABLE_MODELS` defaults to empty, so no model accepts writes until named.
- `delete_object` and `run_action` preview by default and change nothing until
  `confirm=True`.
- Every mutation records a `LogEntry` attributed to the grant's user, with the client
  name in the change message, for example `"Changed status. Via MCP (client: Claude
  Code)."`. A write that cannot record a `LogEntry` rolls back.
- The package's own models, `sessions.Session`, and `authtoken.Token` are never exposed,
  whatever the settings say.
- Keep `auth.Permission` and `auth.Group` out of `WRITABLE_MODELS`. An agent that can
  grant permissions can escape the permission model.

## Deployment

**Separate process.** Run the MCP server next to your Django project:

```bash
python manage.py admin_mcp_serve
```

It serves the path from `MCP_URL`, which is `/admin/mcp` by default, on the port from
`MCP_URL`, or 8765 when that URL names no port. Both are overridable with `--host` and
`--port`. Nothing about your existing serving configuration changes. Route
`/admin/mcp` through your ingress to that port, and make sure the `Authorization`
header passes through.

**Mounted (M3).** Mount the server at `/admin/mcp` inside your project's `asgi.py`. One
constraint: dispatch on the exact path. The OAuth endpoints live directly below the same
prefix (`/admin/mcp/authorize` and friends) and Django must keep serving those, so a
dispatcher that sends everything under `/admin/mcp` to FastMCP would swallow them. The
recipe ships with milestone M3.

The server is stateless, so any instance behind a load balancer can serve any request.

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
