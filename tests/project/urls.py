from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # RFC 8414 fixes this one at the site root.
    path("", include("django_admin_fastmcp.well_known_urls")),
    # The prefix is ours to choose. It matches the path in MCP_URL.
    path("admin/mcp/", include("django_admin_fastmcp.urls")),
    path("admin/", admin.site.urls),
]
