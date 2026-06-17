from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from receipts.forms import EmailOrUsernameAuthenticationForm
from receipts.views import ForcedPasswordChangeView
from django.http import JsonResponse
from django.urls import include, path
from django.views.generic import RedirectView


def healthcheck(request):
    return JsonResponse({"status": "ok"})

urlpatterns = [
    path("health/", healthcheck, name="healthcheck"),
    path("", RedirectView.as_view(pattern_name="dashboard", permanent=False), name="home"),
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="registration/login.html", authentication_form=EmailOrUsernameAuthenticationForm), name="login"),
    path("accounts/password-change/", ForcedPasswordChangeView.as_view(), name="password_change"),
    path("accounts/password-change/done/", auth_views.PasswordChangeDoneView.as_view(template_name="registration/password_change_done.html"), name="password_change_done"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("receipts.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
