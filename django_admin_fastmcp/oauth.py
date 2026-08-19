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
