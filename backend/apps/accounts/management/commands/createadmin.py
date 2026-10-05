from __future__ import annotations

import getpass
import os
from typing import Any

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.accounts.models import User


class Command(BaseCommand):
    help = "Create an admin user (headless alternative to the /setup page)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--email", required=True)
        parser.add_argument(
            "--password-env",
            default="REVIEWBOT_ADMIN_PASSWORD",
            help="Environment variable holding the password (prompted if unset).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        email = options["email"].strip().lower()
        if User.objects.filter(email=email).exists():
            raise CommandError(f"User {email} already exists.")
        password = os.environ.get(options["password_env"]) or getpass.getpass("Password: ")
        try:
            password_validation.validate_password(password, user=User(email=email))
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        User.objects.create_superuser(email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f"Admin {email} created."))
