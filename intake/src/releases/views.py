from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from . import services


@login_required
def index(request):
    feed = services.build_feed(request.user)
    # Visiting the page implicitly marks everything as seen.
    services.mark_all_seen(request.user)
    return render(request, "releases/index.html", feed)


@login_required
@require_http_methods(["POST"])
def mark_seen(request):
    services.mark_all_seen(request.user)
    return redirect("releases:index")
