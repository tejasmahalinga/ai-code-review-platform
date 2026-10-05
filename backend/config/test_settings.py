"""Settings for the test suite: enables testing defaults before the main settings module loads."""

import os

os.environ["REVIEWBOT_TESTING"] = "1"
os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("REVIEWBOT_PUBLIC_URL", "https://reviewbot.example.com")

from config.settings import *  # noqa: F403
