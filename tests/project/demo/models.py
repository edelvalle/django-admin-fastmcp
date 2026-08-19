"""Models built to exercise the permission matrix (SPEC.md section 11)."""

from django.db import models


class Author(models.Model):
    """Registered with a plain ModelAdmin: search_fields only, no overrides."""

    name = models.CharField(max_length=100)
    bio = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Book(models.Model):
    """The workhorse: hidden rows, object-level locks, readonly fields,
    a gated action, and a save_model override that calls message_user."""

    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
    published = models.BooleanField(default=False)
    hidden = models.BooleanField(default=False, help_text="Hidden rows leave the queryset.")
    locked = models.BooleanField(default=False, help_text="Locked rows refuse changes.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


class ApiCredential(models.Model):
    """Password and api_key exercise redaction. Its admin sets mcp_fields
    including the password, to prove the allowlist does not defeat redaction."""

    name = models.CharField(max_length=100)
    password = models.CharField(max_length=128)
    api_key = models.CharField(max_length=128, blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.name


class SecretProject(models.Model):
    """Registered in the admin, opted out of MCP with mcp_expose = False."""

    codename = models.CharField(max_length=100)

    def __str__(self):
        return self.codename


class UnregisteredThing(models.Model):
    """Never registered in the admin, so never exposed over MCP."""

    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name
