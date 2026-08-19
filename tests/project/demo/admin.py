"""ModelAdmins built to exercise the permission matrix (SPEC.md section 11)."""

from django.contrib import admin

from .models import ApiCredential, Author, Book, SecretProject


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    """No overrides at all: default Django permissions apply as-is."""

    search_fields = ("name",)


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = ("title", "author", "published")
    list_filter = ("published",)
    search_fields = ("title",)
    readonly_fields = ("created_at",)
    actions = ("publish_books",)

    def get_queryset(self, request):
        """Row-level scoping: hidden books do not exist for non-superusers."""
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        return queryset.filter(hidden=False)

    def has_change_permission(self, request, obj=None):
        """Object-level override: locked books refuse changes."""
        if obj is not None and obj.locked:
            return False
        return super().has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        self.message_user(request, f"Saved {obj.title!r} through BookAdmin.")

    @admin.action(description="Publish selected books", permissions=["change"])
    def publish_books(self, request, queryset):
        updated = queryset.update(published=True)
        self.message_user(request, f"Published {updated} book(s).")


@admin.register(ApiCredential)
class ApiCredentialAdmin(admin.ModelAdmin):
    # The allowlist names the password on purpose: redaction must win.
    mcp_fields = ("name", "password", "api_key")


@admin.register(SecretProject)
class SecretProjectAdmin(admin.ModelAdmin):
    mcp_expose = False
