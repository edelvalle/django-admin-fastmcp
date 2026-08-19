"""A project that mounts everything somewhere other than /admin/mcp/.

Used by `test_discovery.py` to prove the prefix is the project's choice: the
package hardcodes no path, and the metadata document follows whatever mount
point the project picks.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("django_admin_fastmcp.well_known_urls")),
    path("backoffice/oauth/", include("django_admin_fastmcp.urls")),
    path("backoffice/", admin.site.urls),
]
