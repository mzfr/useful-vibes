from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_migrate_legacy_lists"),
    ]

    operations = [
        migrations.AddField(
            model_name="appsettings",
            name="source_addon_url",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.AddField(
            model_name="appsettings",
            name="qbittorrent_host",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="appsettings",
            name="qbittorrent_port",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appsettings",
            name="qbittorrent_username",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="appsettings",
            name="qbittorrent_password_encrypted",
            field=models.TextField(blank=True, default=""),
        ),
    ]
