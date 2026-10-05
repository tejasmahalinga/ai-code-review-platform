"""Re-encrypts every stored secret with the first key in REVIEWBOT_ENCRYPTION_KEYS.

Rotation procedure:
  1. Generate a new key and prepend it: REVIEWBOT_ENCRYPTION_KEYS=<new>,<old>
  2. Restart the services, then run: python manage.py rotate_encryption_key
  3. Remove the old key from REVIEWBOT_ENCRYPTION_KEYS and restart again.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import models, transaction

from apps.credentials import crypto
from apps.credentials.models import LLMCredential
from apps.repositories.models import GitProviderConnection

ENCRYPTED_FIELDS: list[tuple[type[models.Model], list[str]]] = [
    (LLMCredential, ["encrypted_secret"]),
    (GitProviderConnection, ["encrypted_private_key", "encrypted_webhook_secret", "encrypted_client_secret"]),
]


class Command(BaseCommand):
    help = "Re-encrypt all stored secrets with the current primary encryption key."

    def handle(self, *args: Any, **options: Any) -> None:
        total = 0
        with transaction.atomic():
            for model, fields in ENCRYPTED_FIELDS:
                for obj in model.objects.select_for_update().all():  # type: ignore[attr-defined]
                    changed = []
                    for field in fields:
                        value = getattr(obj, field)
                        if value:
                            setattr(obj, field, crypto.rotate(value))
                            changed.append(field)
                    if changed:
                        obj.save(update_fields=changed)
                        total += len(changed)
        self.stdout.write(self.style.SUCCESS(f"Re-encrypted {total} secret(s)."))
