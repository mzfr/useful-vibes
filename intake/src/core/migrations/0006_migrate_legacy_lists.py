"""Retire the "Watchlist" / "Favorites" CustomLists and drop the lists app tables.

Watchlist membership is already expressed as Status.PLANNING on a tracking
row, and favourites now have their own table, so these two lists were a
duplicate representation left over from the Trakt import. Entries are copied
across before the tables are dropped — nothing is lost silently.

Runs as raw SQL against whatever tables exist, because the lists app is gone
from INSTALLED_APPS: on the production DB the rows may still exist (this
migration does the migration), and on fresh/test databases the tables never
existed at all.
"""
from django.db import migrations

# Which CustomList becomes what.
PLANNING_LISTS = ("Watchlist",)
FAVORITE_LISTS = ("Favorites",)

_TRACKING_TABLE = {
    "movie": "tracking_movie",
    "tv": "tracking_tv",
    "season": "tracking_season",
    "episode": "tracking_episode",
    "book": "tracking_book",
}


def _table_exists(schema_editor, name: str) -> bool:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=%s", [name]
        )
        return cursor.fetchone() is not None


def forwards(apps, schema_editor):
    if not _table_exists(schema_editor, "lists_customlist"):
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT OR IGNORE INTO core_favorite (user_id, item_id, created_at)
            SELECT c.user_id, ci.item_id, COALESCE(ci.date_added, CURRENT_TIMESTAMP)
            FROM lists_customlist c
            JOIN lists_customlistitem ci ON ci.list_id = c.id
            WHERE c.name IN %s
            """,
            [FAVORITE_LISTS],
        )
        # Watchlist membership -> PLANNING rows. Insert only where no row
        # exists; never demote watched / in-progress / paused / dropped.
        for media_type, table in _TRACKING_TABLE.items():
            cursor.execute(
                f"""
                INSERT INTO {table} (item_id, user_id, score, progress, status,
                                     repeats, created_at, updated_at)
                SELECT ci.item_id, c.user_id, NULL, 0, 'planning', 0,
                       COALESCE(ci.date_added, CURRENT_TIMESTAMP),
                       COALESCE(ci.date_added, CURRENT_TIMESTAMP)
                FROM lists_customlist c
                JOIN lists_customlistitem ci ON ci.list_id = c.id
                JOIN core_item i ON i.id = ci.item_id
                WHERE c.name IN %s AND i.media_type = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM {table} t
                      WHERE t.user_id = c.user_id AND t.item_id = ci.item_id
                  )
                """,
                [PLANNING_LISTS, media_type],
            )


def backwards(apps, schema_editor):
    """No-op: the lists app is deleted, restoring it needs its migrations back."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_favorite"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards, hints={"target_db": "default"}),
        # The lists app is gone; drop its tables if they still exist.
        migrations.RunSQL(
            sql="DROP TABLE IF EXISTS lists_customlistitem; DROP TABLE IF EXISTS lists_customlist;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
