from django.conf import settings

from .messages import drain


def app_meta(request):  # noqa: ARG001
    return {
        "APP_NAME": settings.APP_NAME,
        "APP_TAGLINE": settings.APP_TAGLINE,
    }


def user_messages(request):
    """Make the persistent toast queue available to every template.

    Drains on every full-page render (which is what mounts `_toasts.html` from
    `base.html`). HTMX partial responses don't include `base.html` so they
    don't accidentally drain queued messages.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"user_messages": []}
    return {"user_messages": drain(user)}
