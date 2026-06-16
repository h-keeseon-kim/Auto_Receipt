from django.conf import settings


def app_settings(request):
    """Expose small, safe app-level flags to templates."""
    return {"allow_signup": settings.ALLOW_SIGNUP}
