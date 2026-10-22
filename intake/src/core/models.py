"""
Core models: Item (the shared catalog row) and UserMessage (persistent toast queue).

`Item` is keyed by (source, media_type, media_id[, season_number[, episode_number]]).
SQLite CHECK constraints enforce that:

  - episode rows have both season_number and episode_number
  - season rows have season_number and no episode_number
  - everything else (movie / tv / book) has neither
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse

from .enums import MediaType, MessageLevel, Source


class Item(models.Model):
    """A normalized catalog entry. Tracking rows FK into this."""

    source = models.CharField(max_length=32, choices=Source.choices)
    media_type = models.CharField(max_length=16, choices=MediaType.choices)
    media_id = models.CharField(max_length=128)

    # only set for season/episode rows
    season_number = models.IntegerField(null=True, blank=True)
    episode_number = models.IntegerField(null=True, blank=True)

    # Parent links — episodes point at their season, seasons at the show.
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )

    title = models.CharField(max_length=512)
    image = models.URLField(max_length=1024, blank=True, default="")
    synopsis = models.TextField(blank=True, default="")
    release_date = models.DateField(null=True, blank=True)
    runtime = models.IntegerField(null=True, blank=True)  # minutes per ep / movie length
    max_progress = models.IntegerField(
        null=True, blank=True
    )  # episodes in season, pages in book, etc.

    # Cache the raw provider payload for later enrichment without re-fetching.
    raw = models.JSONField(default=dict, blank=True)
    # Durable provider-cache state. `complete` means a successful provider
    # response was persisted; it does not mean metadata can never be refreshed.
    metadata_complete = models.BooleanField(default=False)
    metadata_fetched_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # SQLite treats NULLs as distinct in unique indexes, so a single
            # constraint over the nullable season/episode columns would NOT
            # stop two workers inserting duplicate movie/tv/book rows (and
            # update_or_create would then raise MultipleObjectsReturned).
            # SQLite ignores nulls_distinct=False, so use three partial
            # constraints instead — one per row shape.
            models.UniqueConstraint(
                fields=["source", "media_type", "media_id"],
                condition=Q(season_number__isnull=True) & Q(episode_number__isnull=True),
                name="item_natural_key_root",
            ),
            models.UniqueConstraint(
                fields=["source", "media_type", "media_id", "season_number"],
                condition=Q(season_number__isnull=False) & Q(episode_number__isnull=True),
                name="item_natural_key_season",
            ),
            models.UniqueConstraint(
                fields=["source", "media_type", "media_id", "season_number", "episode_number"],
                condition=Q(season_number__isnull=False) & Q(episode_number__isnull=False),
                name="item_natural_key_episode",
            ),
            models.CheckConstraint(
                name="item_episode_has_both_numbers",
                condition=(
                    ~Q(media_type=MediaType.EPISODE)
                    | (Q(season_number__isnull=False) & Q(episode_number__isnull=False))
                ),
            ),
            models.CheckConstraint(
                name="item_season_has_only_season_number",
                condition=(
                    ~Q(media_type=MediaType.SEASON)
                    | (Q(season_number__isnull=False) & Q(episode_number__isnull=True))
                ),
            ),
            models.CheckConstraint(
                name="item_root_has_no_numbers",
                condition=(
                    Q(media_type__in=[MediaType.EPISODE, MediaType.SEASON])
                    | (Q(season_number__isnull=True) & Q(episode_number__isnull=True))
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["media_type", "source"]),
            models.Index(fields=["parent"]),
        ]

    def __str__(self) -> str:
        if self.media_type == MediaType.EPISODE:
            return f"{self.title} (S{self.season_number:02d}E{self.episode_number:02d})"
        if self.media_type == MediaType.SEASON:
            return f"{self.title} (S{self.season_number:02d})"
        return self.title

    def get_absolute_url(self) -> str:
        """Return the clean public URL for this catalog item.

        TMDB movie and TV IDs usually share one unambiguous numeric route. If
        TMDB reused an ID across both namespaces, TV keeps `/ID/` and the movie
        uses the explicit `/ID/movie/` escape hatch.
        """
        if self.media_type == MediaType.BOOK.value:
            return reverse("tracking:book_detail", args=(self.media_id,))
        if self.source != "tmdb":
            return reverse(
                "tracking:legacy_details",
                args=(self.source, self.media_type, self.media_id),
            )
        if self.media_type == MediaType.EPISODE.value:
            return reverse(
                "tracking:episode_detail",
                args=(self.media_id, self.season_number, self.episode_number),
            )
        if self.media_type == MediaType.SEASON.value:
            return reverse("tracking:details", args=(self.media_id,))
        if (
            self.media_type == MediaType.MOVIE.value
            and Item.objects.filter(
                source="tmdb",
                media_type=MediaType.TV.value,
                media_id=self.media_id,
                season_number__isnull=True,
                episode_number__isnull=True,
            ).exists()
        ):
            return reverse("tracking:movie_details", args=(self.media_id,))
        return reverse("tracking:details", args=(self.media_id,))


def _settings_cipher() -> Fernet:
    """Derive the settings-encryption key from the deployment secret."""
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class AppSettings(models.Model):
    """Single-row application configuration editable from Settings.

    Secret values (API keys, client passwords) are encrypted at rest with a
    key derived from SECRET_KEY. Non-secret values (addon URL, client host)
    stay plaintext so they can be rendered back into the form directly.
    """

    singleton = models.BooleanField(default=True, unique=True, editable=False)
    tmdb_api_key_encrypted = models.TextField(blank=True, default="")
    # Stream lookup (Stremio addon base URL, e.g. https://torrentio.strem.fun)
    source_addon_url = models.CharField(max_length=512, blank=True, default="")
    # Public history sharing.
    history_public_url = models.URLField(max_length=512, blank=True, default="")
    history_sync_interval_value = models.PositiveIntegerField(default=12)
    history_sync_interval_unit = models.CharField(
        max_length=16,
        choices=(("minutes", "Minutes"), ("hours", "Hours")),
        default="hours",
    )
    # Download client (qBittorrent Web API)
    qbittorrent_host = models.CharField(max_length=255, blank=True, default="")
    qbittorrent_port = models.PositiveIntegerField(null=True, blank=True)
    qbittorrent_username = models.CharField(max_length=255, blank=True, default="")
    qbittorrent_password_encrypted = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return "Application settings"

    def set_tmdb_api_key(self, value: str) -> None:
        value = value.strip()
        self.tmdb_api_key_encrypted = (
            _settings_cipher().encrypt(value.encode("utf-8")).decode("ascii") if value else ""
        )

    def get_tmdb_api_key(self) -> str:
        if not self.tmdb_api_key_encrypted:
            return ""
        try:
            return (
                _settings_cipher()
                .decrypt(self.tmdb_api_key_encrypted.encode("ascii"))
                .decode("utf-8")
            )
        except (InvalidToken, ValueError):
            return ""

    def set_secret(self, attr: str, value: str) -> None:
        """Encrypt `value` into the `<attr>_encrypted` column."""
        value = value.strip()
        setattr(
            self,
            f"{attr}_encrypted",
            _settings_cipher().encrypt(value.encode("utf-8")).decode("ascii") if value else "",
        )

    def get_secret(self, attr: str) -> str:
        """Decrypt the `<attr>_encrypted` column; blank on any failure."""
        stored = getattr(self, f"{attr}_encrypted", "")
        if not stored:
            return ""
        try:
            return _settings_cipher().decrypt(stored.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return ""

    @classmethod
    def load(cls) -> AppSettings:
        obj, _ = cls.objects.get_or_create(singleton=True)
        return obj


class Favorite(models.Model):
    """A starred catalog item. Works for any media type, including episodes.

    Deliberately not a flag on the tracking rows: starring an episode should
    not require creating a tracking row for it, and one table keeps the
    /favorites page a single query instead of a five-way union.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="favorites",
    )
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="favorited_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "item"], name="favorite_user_item_unique"),
        ]
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self) -> str:
        return f"★ {self.item}"


class UserMessage(models.Model):
    """Persistent toast queue — survives reloads and HTMX swaps."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    level = models.CharField(
        max_length=16,
        choices=MessageLevel.choices,
        default=MessageLevel.INFO,
    )
    text = models.CharField(max_length=512)
    created_at = models.DateTimeField(auto_now_add=True)
    seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "seen_at"])]

    def __str__(self) -> str:
        return f"[{self.level}] {self.text}"
