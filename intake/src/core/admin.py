from django.contrib import admin

from .models import AppSettings, Item, UserMessage


@admin.register(AppSettings)
class AppSettingsAdmin(admin.ModelAdmin):
    list_display = ("updated_at",)
    exclude = ("tmdb_api_key_encrypted",)


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("title", "media_type", "source", "media_id", "season_number", "episode_number")
    list_filter = ("media_type", "source")
    search_fields = ("title", "media_id")


@admin.register(UserMessage)
class UserMessageAdmin(admin.ModelAdmin):
    list_display = ("user", "level", "text", "created_at", "seen_at")
    list_filter = ("level",)
