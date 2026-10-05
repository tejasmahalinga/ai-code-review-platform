from __future__ import annotations

from typing import Any, ClassVar

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
    # Role enforcement beyond "admin" ships with ADM-02 (P1); the column exists from day one.
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.ADMIN)
    is_active = models.BooleanField(default=True)
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
