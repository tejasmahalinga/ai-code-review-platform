from __future__ import annotations

from typing import Any

from django.contrib.auth import password_validation
from rest_framework import serializers

from apps.accounts.models import User


class UserSerializer(serializers.ModelSerializer[User]):
    class Meta:
        model = User
        fields = ["id", "email", "name", "role", "created_at"]
        read_only_fields = fields


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
