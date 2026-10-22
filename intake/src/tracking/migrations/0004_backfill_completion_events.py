from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import migrations


def forwards(apps, schema_editor):
    Event = apps.get_model("tracking", "CompletionEvent")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Item = apps.get_model("core", "Item")
    valid_users = set(User.objects.values_list("pk", flat=True))
    valid_items = set(Item.objects.values_list("pk", flat=True))
    batch = []
    tz = ZoneInfo(settings.TIME_ZONE)
    for model_name in ("HistoricalMovie", "HistoricalEpisode", "HistoricalBook"):
        Historical = apps.get_model("tracking", model_name)
        previous = {}
        ordinals = {}
        for row in Historical.objects.order_by("id", "history_date", "history_id").iterator(1000):
            if row.user_id not in valid_users or row.item_id not in valid_items:
                continue
            key = row.id
            old_status, old_repeats = previous.get(key, (None, 0))
            repeats = row.repeats or 0
            count = 0
            if row.status == "completed" and row.end_date:
                if old_status != "completed":
                    count += 1
                if repeats > old_repeats:
                    count += repeats - old_repeats
            for _ in range(count):
                ordinal = ordinals.get(key, 0)
                ordinals[key] = ordinal + 1
                batch.append(Event(
                    user_id=row.user_id,
                    item_id=row.item_id,
                    completed_at=datetime.combine(row.end_date, time(12), tzinfo=tz),
                    precision="date",
                    source="legacy_history",
                    source_event_id=f"{model_name}:{key}:{ordinal}",
                ))
            previous[key] = (row.status, repeats)
            if len(batch) >= 500:
                Event.objects.bulk_create(batch, batch_size=500, ignore_conflicts=True)
                batch.clear()
        if batch:
            Event.objects.bulk_create(batch, batch_size=500, ignore_conflicts=True)
            batch.clear()


def backwards(apps, schema_editor):
    apps.get_model("tracking", "CompletionEvent").objects.filter(
        source="legacy_history"
    ).delete()


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("tracking", "0003_completionevent")]
    operations = [migrations.RunPython(forwards, backwards)]
