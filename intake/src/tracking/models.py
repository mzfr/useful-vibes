"""
Per-type tracking tables.

The abstract `Media` holds the shared columns; each concrete subclass adds
constraints specific to its type (an Episode must FK to an episode Item, etc).
Every model gets a `simple_history` log so the rewatch/reread timeline is free.

The `process_progress` / `process_status` hooks keep `progress`, `status`, and
`end_date` consistent on save — the pattern is ported from Yamtrack but with
the manual / scrobbler bits stripped out (we don't have a scrobbler).
"""
from __future__ import annotations

from datetime import date
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from simple_history.models import HistoricalRecords

from core.enums import MediaType, Status
from core.models import Item


class Media(models.Model):
    """Abstract base for every per-type tracking row."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
    )
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")

    score = models.IntegerField(null=True, blank=True)  # 1–10 integer
    progress = models.IntegerField(default=0)
    progressed_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PLANNING
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    repeats = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Subclasses set this to constrain which Item.media_type can FK in.
    REQUIRED_MEDIA_TYPE: ClassVar[str] = ""

    class Meta:
        abstract = True
        constraints = [
            models.CheckConstraint(
                name="%(class)s_score_range",
                condition=Q(score__isnull=True) | (Q(score__gte=1) & Q(score__lte=10)),
            ),
            models.CheckConstraint(
                name="%(class)s_progress_non_negative",
                condition=Q(progress__gte=0),
            ),
            models.CheckConstraint(
                name="%(class)s_repeats_non_negative",
                condition=Q(repeats__gte=0),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.__class__.__name__}<{self.item_id}> ({self.get_status_display()})"

    # ------------------------------------------------------------------ hooks

    def process_progress(self) -> None:
        """Clamp progress to [0, max_progress] when max is known."""
        max_p = self.item.max_progress
        if max_p is not None:
            if self.progress < 0:
                self.progress = 0
            if self.progress > max_p:
                self.progress = max_p

    def process_status(self) -> None:
        """Keep status, end_date, and progress consistent with each other."""
        max_p = self.item.max_progress

        # Completion auto-fills the dates and pegs progress to max.
        if self.status == Status.COMPLETED:
            if max_p is not None:
                self.progress = max_p
            if not self.end_date:
                self.end_date = timezone.localdate()
            if not self.start_date:
                self.start_date = self.end_date

        # Any progress at all means we're in-progress at minimum.
        elif self.status == Status.PLANNING and self.progress > 0:
            self.status = Status.IN_PROGRESS
            if not self.start_date:
                self.start_date = timezone.localdate()

        # Hitting max while watching is implicit completion.
        if (
            self.status == Status.IN_PROGRESS
            and max_p is not None
            and self.progress >= max_p
        ):
            self.status = Status.COMPLETED
            if not self.end_date:
                self.end_date = date.today()

    def save(self, *args, **kwargs):
        if self.REQUIRED_MEDIA_TYPE and self.item.media_type != self.REQUIRED_MEDIA_TYPE:
            raise ValueError(
                f"{self.__class__.__name__} requires Item.media_type="
                f"{self.REQUIRED_MEDIA_TYPE!r}, got {self.item.media_type!r}"
            )
        self.process_progress()
        self.process_status()
        super().save(*args, **kwargs)
        self._ensure_completion_event()

    def _ensure_completion_event(self) -> None:
        """Every completed row must have at least one completion event —
        stats derive from events. record_completion() creates its own event,
        so this only fires for completions written through other paths."""
        from datetime import datetime, time

        from django.utils import timezone

        from .models import CompletionEvent

        if self.status != Status.COMPLETED:
            return
        if CompletionEvent.objects.filter(user=self.user, item=self.item).exists():
            return
        base = self.end_date or timezone.localdate()
        CompletionEvent.objects.create(
            user=self.user,
            item=self.item,
            completed_at=timezone.make_aware(datetime.combine(base, time(12))),
            precision="date",
            source=CompletionEvent.SOURCE_MANUAL,
        )

    def mark_watched_again(self, watched_on: date | None = None) -> None:
        """Record another explicit completion event — a rewatch, not an edit
        to the first one. `watched_on` backdates it; defaults to now."""
        from datetime import datetime, time

        from django.utils import timezone

        from .services import record_completion

        # Rows created before CompletionEvent (or direct admin/test creation)
        # need their original completion seeded before adding the rewatch.
        if not CompletionEvent.objects.filter(user=self.user, item=self.item).exists():
            completed_on = self.end_date or timezone.localdate()
            CompletionEvent.objects.create(
                user=self.user,
                item=self.item,
                completed_at=timezone.make_aware(datetime.combine(completed_on, time(12))),
                source=CompletionEvent.SOURCE_LEGACY,
                source_event_id=f"tracking:{self._meta.label_lower}:{self.pk}:initial",
                precision="date",
            )
        completed_at = (
            timezone.make_aware(datetime.combine(watched_on, time(12)))
            if watched_on else None
        )
        record_completion(
            user=self.user,
            item=self.item,
            completed_at=completed_at,
            precision="date" if watched_on else "instant",
        )
        self.refresh_from_db()


class CompletionEvent(models.Model):
    """One completed watch/read occurrence, independent of current tracking state."""

    SOURCE_MANUAL = "manual"
    SOURCE_TRAKT = "trakt"
    SOURCE_LEGACY = "legacy_history"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="completion_events"
    )
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="completion_events")
    completed_at = models.DateTimeField()
    # Exact instant when known (Trakt/manual); legacy date-only values are
    # anchored at local noon and explicitly marked as date precision.
    precision = models.CharField(
        max_length=8, choices=(("instant", "Instant"), ("date", "Date")), default="instant"
    )
    source = models.CharField(max_length=32, default=SOURCE_MANUAL)
    source_event_id = models.CharField(  # noqa: DJ001 — NULL means a local/manual event
        max_length=128, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "source", "source_event_id"],
                condition=Q(source_event_id__isnull=False),
                name="completion_event_source_identity_unique",
            )
        ]
        indexes = [models.Index(fields=["user", "item", "completed_at"])]

    def __str__(self) -> str:
        return f"{self.item_id} completed at {self.completed_at}"


class ShowFeedPreference(models.Model):
    """Per-user feed visibility for a TV show; never changes watch history."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="show_feed_preferences"
    )
    show = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="feed_preferences")
    up_next_hidden_at = models.DateTimeField(null=True, blank=True)
    releases_hidden_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "show"], name="show_feed_preference_user_show_unique"
            )
        ]

    def __str__(self) -> str:
        return f"Feed preferences for user {self.user_id}, show {self.show_id}"


class ShowAutomaticDownload(models.Model):
    """A show's automatic-download cursor for one user.

    The cursor is captured from Up Next when the option is enabled. This keeps
    older, unwatched seasons from being submitted when the user only wants the
    current episode and everything after it.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="automatic_show_downloads",
    )
    show = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="show_automatic_downloads",
    )
    start_season_number = models.PositiveIntegerField()
    start_episode_number = models.PositiveIntegerField()
    enabled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "show"], name="automatic_download_user_show_unique"
            )
        ]

    def __str__(self) -> str:
        return f"Automatic downloads for user {self.user_id}, show {self.show_id}"


class AutomaticEpisodeDownload(models.Model):
    """Durable delivery state that prevents an episode being submitted twice."""

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_CHOICES = ((STATUS_PENDING, "Pending"), (STATUS_SENT, "Sent"))

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="automatic_episode_downloads",
    )
    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name="automatic_episode_downloads"
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    source_title = models.CharField(max_length=512, blank=True, default="")
    info_hash = models.CharField(max_length=64, blank=True, default="")
    attempted_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "item"], name="automatic_download_user_episode_unique"
            )
        ]

    def __str__(self) -> str:
        return f"Automatic episode download {self.user_id}/{self.item_id}: {self.status}"


# --------------------------------------------------------------------- concrete


class Movie(Media):
    REQUIRED_MEDIA_TYPE = MediaType.MOVIE.value
    history = HistoricalRecords()

    class Meta(Media.Meta):
        abstract = False
        constraints = Media.Meta.constraints + [
            models.UniqueConstraint(fields=["user", "item"], name="movie_user_item_unique"),
        ]


class TV(Media):
    REQUIRED_MEDIA_TYPE = MediaType.TV.value
    history = HistoricalRecords()

    class Meta(Media.Meta):
        abstract = False
        verbose_name = "TV show tracking"
        verbose_name_plural = "TV show tracking"
        constraints = Media.Meta.constraints + [
            models.UniqueConstraint(fields=["user", "item"], name="tv_user_item_unique"),
        ]


class Season(Media):
    REQUIRED_MEDIA_TYPE = MediaType.SEASON.value
    history = HistoricalRecords()

    class Meta(Media.Meta):
        abstract = False
        constraints = Media.Meta.constraints + [
            models.UniqueConstraint(fields=["user", "item"], name="season_user_item_unique"),
        ]


class Episode(Media):
    """Episode tracking — the bedrock of the episode grid / Up Next."""

    REQUIRED_MEDIA_TYPE = MediaType.EPISODE.value
    note = models.TextField(blank=True, default="")  # private per-episode note
    history = HistoricalRecords()

    class Meta(Media.Meta):
        abstract = False
        constraints = Media.Meta.constraints + [
            models.UniqueConstraint(fields=["user", "item"], name="episode_user_item_unique"),
        ]


class Book(Media):
    REQUIRED_MEDIA_TYPE = MediaType.BOOK.value
    history = HistoricalRecords()

    class Meta(Media.Meta):
        abstract = False
        constraints = Media.Meta.constraints + [
            models.UniqueConstraint(fields=["user", "item"], name="book_user_item_unique"),
        ]


# Convenience: which model handles which media_type.
MODEL_FOR_MEDIA_TYPE: dict[str, type[Media]] = {
    MediaType.MOVIE.value: Movie,
    MediaType.TV.value: TV,
    MediaType.SEASON.value: Season,
    MediaType.EPISODE.value: Episode,
    MediaType.BOOK.value: Book,
}
