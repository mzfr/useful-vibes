from django.contrib import admin

from .models import ReleaseCheck


@admin.register(ReleaseCheck)
class ReleaseCheckAdmin(admin.ModelAdmin):
    list_display = ("item", "user", "last_checked_at", "seen_at")
