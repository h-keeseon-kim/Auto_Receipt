from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.urls import NoReverseMatch, reverse


TUTORIAL_EXEMPT_URL_NAMES = {
    "login",
    "logout",
    "register",
    "password_change",
    "password_change_done",
    "tutorial_complete",
    "healthcheck",
}


def tutorial_context(request) -> tuple[bool, bool, str]:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False, False, ""

    try:
        profile = user.profile
    except ObjectDoesNotExist:
        return False, False, ""

    if profile.must_change_password:
        return False, False, ""

    try:
        complete_url = reverse("tutorial_complete")
    except NoReverseMatch:
        complete_url = ""

    resolver_match = getattr(request, "resolver_match", None)
    url_name = getattr(resolver_match, "url_name", "")
    auto_start = profile.tutorial_completed_at is None and url_name not in TUTORIAL_EXEMPT_URL_NAMES
    return True, auto_start, complete_url


def app_settings(request):
    """Expose small, safe app-level flags to templates."""
    tutorial_available, tutorial_auto_start, tutorial_complete_url = tutorial_context(request)
    return {
        "allow_signup": settings.ALLOW_SIGNUP,
        "app_name": getattr(settings, "APP_NAME", "ReceiptHub"),
        "tutorial_available": tutorial_available,
        "tutorial_auto_start": tutorial_auto_start,
        "tutorial_complete_url": tutorial_complete_url,
    }
