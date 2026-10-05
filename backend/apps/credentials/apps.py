from django.apps import AppConfig


class CredentialsConfig(AppConfig):
    name = "apps.credentials"
    label = "credentials"

    def ready(self) -> None:
        # Fail fast at startup if the master encryption key is missing or malformed.
        from apps.credentials import crypto

        crypto.check_configuration()
