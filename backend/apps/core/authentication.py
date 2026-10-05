from rest_framework.authentication import SessionAuthentication as DRFSessionAuthentication
from rest_framework.request import Request


class SessionAuthentication(DRFSessionAuthentication):
    """Session auth that answers unauthenticated requests with 401 (not 403)."""

    def authenticate_header(self, request: Request) -> str:
        return 'Session realm="api"'
