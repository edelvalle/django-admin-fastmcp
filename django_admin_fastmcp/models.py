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

SCOPE_READ = "admin:read"
SCOPE_WRITE = "admin:write"


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
    rotates on every use. A grant is as privileged as its scopes allow and
    never more than its user.
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

    class Meta:
        permissions = [("write_via_mcp", "Can obtain the admin:write scope over MCP")]

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
