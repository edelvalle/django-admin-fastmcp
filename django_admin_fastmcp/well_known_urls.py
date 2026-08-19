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
