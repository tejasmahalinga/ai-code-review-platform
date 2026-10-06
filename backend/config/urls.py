from django.urls import URLPattern, URLResolver, include, path
from drf_spectacular.views import SpectacularAPIView
from rest_framework.routers import SimpleRouter

from apps.accounts import views as accounts
from apps.audit import views as audit
from apps.core import views as core
from apps.credentials import views as credentials
from apps.notifications import views as notifications
from apps.repositories import views as repositories
from apps.reviews import views as reviews
from apps.webhooks import views as webhooks

router = SimpleRouter(trailing_slash=False)
router.register("llm-credentials", credentials.LLMCredentialViewSet, basename="llm-credential")
router.register("repositories", repositories.RepositoryViewSet, basename="repository")
router.register("pull-requests", reviews.PullRequestViewSet, basename="pull-request")
router.register("reviews", reviews.ReviewRunViewSet, basename="review")
router.register("findings", reviews.FindingViewSet, basename="finding")
router.register("webhook-deliveries", webhooks.WebhookDeliveryViewSet, basename="webhook-delivery")
router.register("users", accounts.UserViewSet, basename="user")
router.register("invites", accounts.InviteViewSet, basename="invite")
router.register("audit-events", audit.AuditEventViewSet, basename="audit-event")
router.register("auth/tokens", accounts.ApiTokenViewSet, basename="api-token")
router.register("model-prices", credentials.ModelPriceViewSet, basename="model-price")
router.register(
    "notification-channels", notifications.NotificationChannelViewSet, basename="notification-channel"
)

api_v1: list[URLPattern | URLResolver] = [
    path("auth/csrf", accounts.CsrfView.as_view()),
    path("auth/login", accounts.LoginView.as_view()),
    path("auth/logout", accounts.LogoutView.as_view()),
    path("auth/me", accounts.MeView.as_view()),
    path("auth/me/github", accounts.GitHubUnlinkView.as_view()),
    path("auth/password", accounts.PasswordChangeView.as_view()),
    path("auth/github/start", accounts.GitHubLoginStartView.as_view()),
    path("auth/github/callback", accounts.GitHubLoginCallbackView.as_view()),
    path("auth/invites/<str:token>", accounts.InvitePublicView.as_view()),
    path("setup/status", accounts.SetupStatusView.as_view()),
    path("setup", accounts.SetupView.as_view()),
    path("llm-providers", credentials.LLMProvidersView.as_view()),
    path("integrations/github", repositories.GitHubIntegrationView.as_view()),
    path("integrations/github/manual", repositories.GitHubManualConfigView.as_view()),
    path("integrations/github/manifest", repositories.GitHubManifestView.as_view()),
    path("integrations/github/callback", repositories.GitHubCallbackView.as_view()),
    path("integrations/github/installed", repositories.GitHubInstalledView.as_view()),
    path("integrations/github/sync", repositories.GitHubSyncView.as_view()),
    path("usage", reviews.UsageView.as_view()),
    path("review-profiles", reviews.ReviewProfilesView.as_view()),
    path("feedback-stats", reviews.FeedbackStatsView.as_view()),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("", include(router.urls)),
]

urlpatterns = [
    path("api/v1/", include(api_v1)),
    path("webhooks/github", webhooks.github_webhook),
    path("healthz", core.healthz),
    path("readyz", core.readyz),
    path("metrics", core.metrics),
]
