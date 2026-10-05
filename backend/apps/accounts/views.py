from __future__ import annotations

from typing import Any, cast

from django.contrib.auth import authenticate, login, logout
from django.db import connection, transaction
from django.http import Http404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.serializers import LoginSerializer, SetupSerializer, UserSerializer
from apps.accounts.throttles import LoginThrottle, SetupThrottle
from apps.core.logging import get_logger

logger = get_logger(__name__)

# Arbitrary constant used as a Postgres advisory-lock id so concurrent setup requests serialize.
_SETUP_LOCK_ID = 7_301_001


@method_decorator(ensure_csrf_cookie, name="get")
class CsrfView(APIView):
    """Sets the ``csrftoken`` cookie. The dashboard calls this once before any unsafe request."""

    permission_classes = [AllowAny]

    @extend_schema(responses={204: None})
    def get(self, request: Request) -> Response:
        return Response(status=status.HTTP_204_NO_CONTENT)


class SetupStatusView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: {"type": "object", "properties": {"needs_setup": {"type": "boolean"}}}})
    def get(self, request: Request) -> Response:
        return Response({"needs_setup": not User.objects.exists()})


class SetupView(APIView):
    """Creates the first admin. Only available while the instance has zero users."""

    permission_classes = [AllowAny]
    throttle_classes = [SetupThrottle]

    @extend_schema(request=SetupSerializer, responses={201: UserSerializer})
    def post(self, request: Request) -> Response:
        if User.objects.exists():
            raise Http404
        serializer = SetupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data: dict[str, Any] = serializer.validated_data
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_SETUP_LOCK_ID])
            if User.objects.exists():
                raise Http404
            user = User.objects.create_superuser(
                email=data["email"], password=data["password"], name=data.get("name", "")
            )
        login(request._request, user, backend="django.contrib.auth.backends.ModelBackend")
        logger.info("setup.admin_created", user_id=user.pk)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    @extend_schema(request=LoginSerializer, responses={200: UserSerializer})
    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request._request,
            username=serializer.validated_data["email"].lower(),
            password=serializer.validated_data["password"],
        )
        if user is None:
            LoginThrottle().record_failure(request, self)
            logger.info("auth.login_failed")
            return Response(
                {
                    "error": {
                        "code": "invalid_credentials",
                        "message": "Invalid email or password.",
                        "details": None,
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        login(request._request, user)
        logger.info("auth.login", user_id=user.pk)
        return Response(UserSerializer(user).data)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={204: None})
    def post(self, request: Request) -> Response:
        logout(request._request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    @extend_schema(responses={200: UserSerializer})
    def get(self, request: Request) -> Response:
        return Response(UserSerializer(cast(User, request.user)).data)
