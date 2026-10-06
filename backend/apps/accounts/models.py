from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any, ClassVar

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        if not email:
            raise ValueError("Email is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("role", User.Role.ADMIN)
        extra.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        REVIEWER = "reviewer", "Reviewer"
        VIEWER = "viewer", "Viewer"

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    is_active = models.BooleanField(default=True)
    # Linked GitHub identity (ADM-03). The numeric id is stable; the login can change on GitHub.
    github_id = models.BigIntegerField(null=True, blank=True, unique=True)
    github_login = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[UserManager] = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.email

    @property
    def is_staff(self) -> bool:
        return self.role == self.Role.ADMIN

    @property
    def is_admin(self) -> bool:
        return self.role == self.Role.ADMIN

    @property
    def can_review(self) -> bool:
        """Admins and reviewers can trigger reviews, triage findings and edit review rules."""
        return self.role in (self.Role.ADMIN, self.Role.REVIEWER)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class InviteQuerySet(models.QuerySet["Invite"]):
    def pending(self) -> InviteQuerySet:
        return self.filter(accepted_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now())


class Invite(models.Model):
    """A single-use invitation link (ADM-02). Only the SHA-256 of the token is stored."""

    email = models.EmailField()
    role = models.CharField(max_length=16, choices=User.Role.choices, default=User.Role.VIEWER)
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = InviteQuerySet.as_manager()

    class Meta:
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"invite:{self.email}"

    @property
    def audit_label(self) -> str:
        return self.email

    @classmethod
    def issue(cls, *, email: str, role: str, created_by: User | None) -> tuple[Invite, str]:
        """Creates an invite and returns it with the plaintext token (shown once)."""
        token = secrets.token_urlsafe(32)
        invite = cls.objects.create(
            email=email.strip().lower(),
            role=role,
            token_hash=hash_token(token),
            created_by=created_by,
            expires_at=timezone.now() + timedelta(hours=settings.INVITE_TTL_HOURS),
        )
        return invite, token

    @classmethod
    def find_pending(cls, token: str) -> Invite | None:
        if not token:
            return None
        return cls.objects.pending().filter(token_hash=hash_token(token)).first()

    @property
    def status(self) -> str:
        if self.accepted_at:
            return "accepted"
        if self.revoked_at:
            return "revoked"
        if self.expires_at <= timezone.now():
            return "expired"
        return "pending"
