from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "login",
        auth_views.LoginView.as_view(template_name="core/login.html"),
        name="login",
    ),
    path("logout", auth_views.LogoutView.as_view(), name="logout"),
    path("releases/", include("releases.urls")),
    path("", include("stats.urls")),
    path("", include("imports.urls")),
    path("", include("api.urls")),
    path("", include("sources.urls")),
    path("", include("sharing.urls")),
    path("", include("tracking.urls")),
    path("", include("core.urls")),
]
