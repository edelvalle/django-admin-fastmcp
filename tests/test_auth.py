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
