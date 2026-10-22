from django.contrib import admin

from .models import APIToken


@admin.register(APIToken)
class APITokenAdmin(admin.ModelAdmin):
    list_display = ("user", "label", "expires_at", "last_used_at", "created_at")
