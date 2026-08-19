"""Read-only audit changelists for McpClient and McpToken (SPEC.md section 8).

Who authorized which client, with which scopes, last used when. Revocation is
an admin action. Nothing is created by hand: tokens are issued only by the
OAuth flow. Staff see their own grants, superusers see all.
"""

from django.contrib import admin

from django_admin_fastmcp.models import McpClient, McpToken


class ReadOnlyAdmin(admin.ModelAdmin):
    mcp_expose = False  # belt and suspenders on top of the built-in denylist

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(McpClient)
class McpClientAdmin(ReadOnlyAdmin):
    list_display = ("name", "client_id", "created_at")
    search_fields = ("name", "client_id")


@admin.register(McpToken)
class McpTokenAdmin(ReadOnlyAdmin):
    list_display = (
        "client",
        "user",
        "scope_list",
        "access_expires_at",
        "refresh_expires_at",
        "revoked_at",
        "last_used_at",
    )
    list_filter = ("revoked_at",)
    actions = ("revoke_grants",)

    @admin.display(description="scopes")
    def scope_list(self, obj):
        return ", ".join(obj.scopes)

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related("user", "client")
        if request.user.is_superuser:
            return queryset
        return queryset.filter(user=request.user)

    @admin.action(description="Revoke selected grants", permissions=["view"])
    def revoke_grants(self, request, queryset):
        count = 0
        for grant in queryset:
            grant.revoke()
            count += 1
        self.message_user(request, f"Revoked {count} grant(s).")
