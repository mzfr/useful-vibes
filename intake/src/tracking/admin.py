from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    TV,
    AutomaticEpisodeDownload,
    Book,
    Episode,
    Movie,
    Season,
    ShowAutomaticDownload,
)


@admin.register(Movie)
class MovieAdmin(SimpleHistoryAdmin):
    list_display = ("item", "user", "status", "score", "repeats", "end_date")
    list_filter = ("status",)


@admin.register(TV)
class TVAdmin(SimpleHistoryAdmin):
    list_display = ("item", "user", "status", "score", "repeats", "progress")
    list_filter = ("status",)


@admin.register(Season)
class SeasonAdmin(SimpleHistoryAdmin):
    list_display = ("item", "user", "status", "score", "repeats", "progress")
    list_filter = ("status",)


@admin.register(Episode)
class EpisodeAdmin(SimpleHistoryAdmin):
    list_display = ("item", "user", "status", "score", "repeats")
    list_filter = ("status",)


@admin.register(Book)
class BookAdmin(SimpleHistoryAdmin):
    list_display = ("item", "user", "status", "score", "progress", "repeats")
    list_filter = ("status",)


@admin.register(ShowAutomaticDownload)
class ShowAutomaticDownloadAdmin(admin.ModelAdmin):
    list_display = ("show", "user", "start_season_number", "start_episode_number", "enabled_at")


@admin.register(AutomaticEpisodeDownload)
class AutomaticEpisodeDownloadAdmin(admin.ModelAdmin):
    list_display = ("item", "user", "status", "attempted_at", "sent_at")
    list_filter = ("status",)
