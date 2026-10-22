"""
First-boot single-user bootstrap.

After migrations, if the user table is empty and INTAKE_INITIAL_PASSWORD is set,
create the admin user. The env var is ignored on subsequent boots.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models.signals import post_migrate
from django.dispatch import receiver

log = logging.getLogger(__name__)


@receiver(post_migrate)
def bootstrap_admin_user(sender, **kwargs) -> None:  # noqa: ARG001
    # Only run when the auth app migrations finish, otherwise the user table
    # may not exist yet during a fresh migrate.
    if getattr(sender, "label", "") != "core":
        return

    password = getattr(settings, "INTAKE_INITIAL_PASSWORD", "") or ""
    username = getattr(settings, "INTAKE_INITIAL_USERNAME", "admin")

    User = get_user_model()
    if User.objects.exists():
        return
    if not password:
        log.warning(
            "No users exist and INTAKE_INITIAL_PASSWORD is unset; "
            "skipping admin bootstrap. Set it and re-run migrate."
        )
        return

    User.objects.create_superuser(username=username, password=password)
    log.info("Created initial admin user %r.", username)
