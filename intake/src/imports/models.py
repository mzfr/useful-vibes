"""
ImportJob row backs the HTMX progress bar. Single user, so one row per run is
fine — no concurrency, no queue. Importers update `processed` as they go.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class ImportJob(models.Model):
    KIND_CHOICES = (
        ("trakt", "Trakt"),
        ("csv_movies_shows", "CSV (movies + shows)"),
        ("csv_books", "CSV (books)"),
    )
    STATE_CHOICES = (
        ("queued", "Queued"),
        ("running", "Running"),
        ("done", "Done"),
        ("failed", "Failed"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="import_jobs"
    )
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default="queued")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    processed = models.IntegerField(default=0)
    total = models.IntegerField(default=0)
    error = models.TextField(blank=True, default="")
    # rows that couldn't be matched to a provider Item — surfaced to the user
    needs_review = models.JSONField(default=list, blank=True)
    archive_fingerprint = models.CharField(  # noqa: DJ001 — NULL enables partial uniqueness
        max_length=64, null=True, blank=True
    )
    phase = models.CharField(max_length=32, default="queued")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind", "archive_fingerprint"],
                condition=models.Q(archive_fingerprint__isnull=False),
                name="import_job_archive_identity_unique",
            )
        ]

    def __str__(self) -> str:
        return f"ImportJob #{self.pk} ({self.kind}, {self.state})"

    def start(self) -> None:
        from django.utils import timezone

        self.state = "running"
        self.started_at = timezone.now()
        self.save(update_fields=["state", "started_at"])

    def fail(self, error: str) -> None:
        from django.utils import timezone

        self.state = "failed"
        self.error = error
        self.finished_at = timezone.now()
        self.save()

    def finish(self, *, processed: int, needs_review: list[dict]) -> None:
        from django.utils import timezone

        self.processed = processed
        self.needs_review = needs_review
        self.state = "done"
        self.finished_at = timezone.now()
        self.save()

    def save_progress(self, *, every: int = 5) -> None:
        if self.processed % every == 0:
            self.save(update_fields=["processed"])

    @property
    def pending_review(self) -> list[dict]:
        return [entry for entry in self.needs_review if not entry.get("resolution")]

    @property
    def pending_review_count(self) -> int:
        return len(self.pending_review)

    @property
    def resolved_review_count(self) -> int:
        return len(self.needs_review) - self.pending_review_count

    @property
    def percent(self) -> int:
        if not self.total:
            return 0
        return min(100, int(100 * self.processed / self.total))

    @classmethod
    def fail_stale_running(cls, user, *, max_age_hours: int = 2) -> int:
        """Mark `running` jobs older than `max_age_hours` as failed.

        Covers hard crashes (SIGKILL, OOM, container restart) that no
        in-process exception handler can catch. Called from the imports index
        view — cheap, no scheduler needed.
        """
        from datetime import timedelta

        from django.utils import timezone

        cutoff = timezone.now() - timedelta(hours=max_age_hours)
        return cls.objects.filter(
            user=user, state="running", started_at__lt=cutoff
        ).update(
            state="failed",
            error="Job aborted — the process died mid-import (timeout/restart).",
            finished_at=timezone.now(),
        )


class TraktLegacy(models.Model):
    """Whitelisted archival Trakt account metadata, displayed but not queried."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trakt_legacy"
    )
    metadata = models.JSONField(default=dict)
    imported_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Trakt legacy for user {self.user_id}"
