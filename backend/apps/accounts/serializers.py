from __future__ import annotations

from typing import Any

from django.contrib.auth import password_validation
from rest_framework import serializers

from apps.accounts.models import ApiToken, Invite, User


class UserSerializer(serializers.ModelSerializer[User]):
    has_password = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "name",
            "role",
            "is_active",
            "github_login",
            "has_password",
            "last_login",
            "created_at",
        ]
        read_only_fields = fields

    def get_has_password(self, obj: User) -> bool:
        return obj.has_usable_password()


class UserUpdateSerializer(serializers.ModelSerializer[User]):
    """What an admin may change on another account."""

    class Meta:
        model = User
        fields = ["name", "role", "is_active"]


class MeUpdateSerializer(serializers.ModelSerializer[User]):
    class Meta:
        model = User
        fields = ["name"]


class PasswordChangeSerializer(serializers.Serializer[Any]):
    current_password = serializers.CharField(
        write_only=True, max_length=256, required=False, allow_blank=True
    )
    new_password = serializers.CharField(write_only=True, min_length=10, max_length=256)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        user: User = self.context["user"]
        if user.has_usable_password() and not user.check_password(attrs.get("current_password", "")):
            raise serializers.ValidationError({"current_password": "The current password is incorrect."})
        password_validation.validate_password(attrs["new_password"], user=user)
        return attrs


class SetupSerializer(serializers.Serializer[Any]):
    email = serializers.EmailField()
    name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, min_length=10, max_length=256)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        candidate = User(email=attrs["email"], name=attrs.get("name", ""))
        password_validation.validate_password(attrs["password"], user=candidate)
        return attrs


class LoginSerializer(serializers.Serializer[Any]):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, max_length=256)


class InviteSerializer(serializers.ModelSerializer[Invite]):
    status = serializers.CharField(read_only=True)
    created_by_email = serializers.SerializerMethodField()

    class Meta:
        model = Invite
        fields = [
            "id",
            "email",
            "role",
            "status",
            "created_by_email",
            "created_at",
            "expires_at",
            "accepted_at",
        ]
        read_only_fields = ["id", "status", "created_by_email", "created_at", "expires_at", "accepted_at"]

    def get_created_by_email(self, obj: Invite) -> str:
        return obj.created_by.email if obj.created_by else ""

    def validate_email(self, value: str) -> str:
        email = value.strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return email


class InviteAcceptSerializer(serializers.Serializer[Any]):
    name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, min_length=10, max_length=256)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        invite: Invite = self.context["invite"]
        candidate = User(email=invite.email, name=attrs.get("name", ""))
        password_validation.validate_password(attrs["password"], user=candidate)
        return attrs


class ApiTokenSerializer(serializers.ModelSerializer[ApiToken]):
    expires_in_days = serializers.ChoiceField(
        choices=[7, 30, 90, 365], required=False, allow_null=True, write_only=True
    )
    active = serializers.BooleanField(source="is_active", read_only=True)

    class Meta:
        model = ApiToken
        fields = [
            "id",
            "name",
            "hint",
            "created_at",
            "expires_at",
            "last_used_at",
            "revoked_at",
            "active",
            "expires_in_days",
        ]
        read_only_fields = ["id", "hint", "created_at", "expires_at", "last_used_at", "revoked_at", "active"]
