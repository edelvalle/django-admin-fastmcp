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
