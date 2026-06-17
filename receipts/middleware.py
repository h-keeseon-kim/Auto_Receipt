from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse


class ForcePasswordChangeMiddleware:
    """初期パスワードのままの一般ユーザーをパスワード変更ページへ固定する。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        redirect_response = self._redirect_if_required(request)
        if redirect_response is not None:
            return redirect_response
        return self.get_response(request)

    def _redirect_if_required(self, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if user.is_staff:
            return None

        profile = getattr(user, "profile", None)
        if profile is None or not profile.must_change_password:
            return None

        try:
            password_change_url = reverse("password_change")
            logout_url = reverse("logout")
            healthcheck_url = reverse("healthcheck")
            tutorial_complete_url = reverse("tutorial_complete")
        except NoReverseMatch:
            return None

        path = request.path
        exempt_paths = {password_change_url, logout_url, healthcheck_url, tutorial_complete_url}
        if path in exempt_paths:
            return None
        if path.startswith(getattr(settings, "STATIC_URL", "/static/")):
            return None
        if path.startswith(getattr(settings, "MEDIA_URL", "/media/")):
            return None

        if not request.session.get("password_change_notice_shown"):
            messages.warning(request, "初回ログインのため、機能を使う前にパスワードを変更してください。")
            request.session["password_change_notice_shown"] = True
        return redirect(password_change_url)
