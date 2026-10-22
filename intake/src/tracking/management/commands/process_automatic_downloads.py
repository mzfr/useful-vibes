from django.core.management.base import BaseCommand

from sources.automatic import process_all_subscriptions


class Command(BaseCommand):
    help = "Check opted-in shows and submit newly aired episodes to the download client."

    def handle(self, *args, **options):
        result = process_all_subscriptions()
        self.stdout.write(
            f"Checked {result.checked}; sent {result.sent}; "
            f"skipped {result.skipped}; failed {result.failed}."
        )
        for error in result.errors:
            self.stderr.write(error)
