from django.contrib import admin

from .models import AccessLink


@admin.register(AccessLink)
class AccessLinkAdmin(admin.ModelAdmin):
    list_display = ("label", "scope", "owner", "expires_at", "revoked_at", "visit_count")
    list_filter = ("scope",)
