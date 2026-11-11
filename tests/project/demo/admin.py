"""ModelAdmins built to exercise the permission matrix (SPEC.md section 11).

Postponed annotations on purpose: a declared tool must resolve its hints in this module.
"""

from __future__ import annotations

from datetime import datetime

from django.contrib import admin
from django.core.exceptions import ValidationError

from django_admin_fastmcp.tools.declared import mcp_tool

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

    @mcp_tool(write=True)
    def retitle(self, request, pk: str, title: str) -> Book:
        """Give a book a new title."""
        book = self.get_queryset(request).get(pk=pk)
        if not self.has_change_permission(request, book):
            raise PermissionError("locked")
        if not title.strip():
            raise ValidationError({"title": ["A title cannot be empty."]})
        book.title = title
        book.save(update_fields=["title"])
        self.message_user(request, f"Retitled {book.pk} through BookAdmin.")
        return book

    @mcp_tool
    def count_books(
        self, request, published: bool | None = None, created_after: datetime | None = None
    ) -> dict:
        """How many books the caller can see, published or not."""
        queryset = self.get_queryset(request)
        if published is not None:
            queryset = queryset.filter(published=published)
        if created_after is not None:
            queryset = queryset.filter(created_at__gt=created_after)
        return {"count": queryset.count()}


@admin.register(ApiCredential)
class ApiCredentialAdmin(admin.ModelAdmin):
    # The allowlist names the password on purpose: redaction must win.
    mcp_fields = ("name", "password", "api_key")


@admin.register(SecretProject)
class SecretProjectAdmin(admin.ModelAdmin):
    mcp_expose = False
