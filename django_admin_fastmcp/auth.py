"""DjangoAdminTokenVerifier, RemoteAuthProvider wiring, current_user() (SPEC.md section 8).

The MCP server side of auth: a FastMCP `RemoteAuthProvider` composes the
verifier with RFC 9728 protected-resource metadata naming the Django site as
the authorization server.
"""

import contextvars
import functools
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import close_old_connections
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

# Test hook: the in-memory MCP transport carries no HTTP bearer token, so the
# test suite impersonates a user here. Production calls never set it.
_impersonated: contextvars.ContextVar = contextvars.ContextVar(
    "django_admin_fastmcp_user", default=None
)


def with_fresh_connections(fn):
    """Wrap the sync `fn` in the connection lifecycle of a Django request.

    FastMCP is not Django's request handler, so the `request_started` and
    `request_finished` signals never fire and nothing else closes stale
    connections.  Without this, each worker thread keeps its first connection
    forever: a broken one stays broken, `CONN_MAX_AGE` and health checks never
    apply, and a pooled one never returns to the pool.  The wrapper must run
    in the worker thread that owns the connection, not on the event loop.

    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        close_old_connections()
        try:
            return fn(*args, **kwargs)
        finally:
            close_old_connections()

    return wrapper


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
        return await sync_to_async(with_fresh_connections(self.verify_token_sync))(token)

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
        throttle_minutes = conf.get("LAST_USED_THROTTLE_MINUTES")
        if throttle_minutes > 0 and (
            grant.last_used_at is None
            or now - grant.last_used_at > timedelta(minutes=throttle_minutes)
        ):
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
