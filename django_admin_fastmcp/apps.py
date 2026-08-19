from django.apps import AppConfig


class DjangoAdminFastmcpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_admin_fastmcp"
    verbose_name = "Admin MCP"

    def ready(self):
        # Import registers the system checks (SPEC.md section 3).
        from django_admin_fastmcp import checks  # noqa: F401
