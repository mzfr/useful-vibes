from django.contrib import admin

from .models import ImportJob


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = ("user", "kind", "state", "processed", "total", "started_at", "finished_at")
    list_filter = ("kind", "state")
