"""Render active history share links to a static directory."""

from pathlib import Path

from django.core.management.base import BaseCommand

from sharing.static_history import render_static_history


class Command(BaseCommand):
    help = "Render active history share links as a static Cloudflare Pages site"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=Path,
            required=True,
            help="Directory to replace with the generated static site",
        )

    def handle(self, *args, **options):
        result = render_static_history(options["output"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Rendered {result['pages']} pages for {result['links']} active links"
            )
        )
