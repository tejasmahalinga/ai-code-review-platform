from django.urls import URLPattern, URLResolver, include, path
from drf_spectacular.views import SpectacularAPIView
from rest_framework.routers import SimpleRouter

from apps.accounts import views as accounts
from apps.core import views as core
from apps.credentials import views as credentials

router = SimpleRouter(trailing_slash=False)
router.register("llm-credentials", credentials.LLMCredentialViewSet, basename="llm-credential")

api_v1: list[URLPattern | URLResolver] = [
    path("auth/csrf", accounts.CsrfView.as_view()),
    path("auth/login", accounts.LoginView.as_view()),
    path("auth/logout", accounts.LogoutView.as_view()),
    path("auth/me", accounts.MeView.as_view()),
    path("setup/status", accounts.SetupStatusView.as_view()),
    path("setup", accounts.SetupView.as_view()),
    path("llm-providers", credentials.LLMProvidersView.as_view()),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("", include(router.urls)),
]

urlpatterns = [
    path("api/v1/", include(api_v1)),
    path("healthz", core.healthz),
    path("readyz", core.readyz),
    path("metrics", core.metrics),
]
