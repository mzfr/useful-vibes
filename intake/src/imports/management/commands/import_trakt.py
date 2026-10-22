"""Import a Trakt data export from the command line (for scripted backfills)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from imports.trakt_export import extract_export, import_legacy_and_hidden, run_offline_import


class Command(BaseCommand):
    help = (
        "Import a Trakt data export (ZIP or unzipped folder) downloaded from "
        "https://trakt.tv/settings/data"
    )

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the export ZIP or folder")
        parser.add_argument("--username", default="admin")

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(username=opts["username"])
        except User.DoesNotExist as exc:
            raise CommandError(f"No user named {opts['username']!r}") from exc

        source = Path(opts["path"])
        if not source.exists():
            raise CommandError(f"No such path: {source}")

        with tempfile.TemporaryDirectory() as tmp:
            folder = extract_export(source, Path(tmp) / "extracted")
            job = run_offline_import(user, folder)
            if job.state == "failed":
                raise CommandError(job.error or "Trakt import failed")
            extras = import_legacy_and_hidden(user, folder)
            self.stdout.write(
                f"Hidden shows: {extras['hidden_shows']}, "
                f"errors: {len(extras['errors'])}"
            )

        self.stdout.write(self.style.SUCCESS(
            f"Trakt import finished: state={job.state} "
            f"processed={job.processed}/{job.total} "
            f"needs_review={len(job.needs_review)}"
        ))
        if job.error:
            self.stdout.write(self.style.ERROR(job.error))
