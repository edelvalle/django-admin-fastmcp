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
