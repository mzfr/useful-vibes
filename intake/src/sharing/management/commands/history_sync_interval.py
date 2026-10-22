"""Print the configured static-history sync interval in seconds."""

from django.core.management.base import BaseCommand

from core.settings_store import history_sync_interval


class Command(BaseCommand):
    help = "Print the configured history sync interval in seconds"

    def handle(self, *args, **options):
        _value, _unit, seconds = history_sync_interval()
        self.stdout.write(str(seconds))
