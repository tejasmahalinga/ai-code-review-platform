from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.reviews import evaluation

DEFAULT_CASES = Path(settings.BASE_DIR) / "evals" / "cases"


class Command(BaseCommand):
    help = "Scores the review engine on labelled example pull requests (see docs/evaluation.md)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="Directory of case YAML files.")
        parser.add_argument("--case", action="append", default=[], help="Run only this case (repeatable).")
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--credential", type=int, help="Use a stored LLM key (id from the dashboard).")
        source.add_argument("--provider", help="openai | anthropic | openai_compatible | fake")
        parser.add_argument("--model", default="", help="Model (default: the credential's model).")
        parser.add_argument(
            "--api-key-env",
            default="REVIEWBOT_EVAL_API_KEY",
            help="Environment variable holding the API key.",
        )
        parser.add_argument("--base-url", default="", help="Base URL for OpenAI-compatible endpoints.")
        parser.add_argument("--profile", default="balanced")
        parser.add_argument("--no-context", action="store_true", help="Disable surrounding-code context.")
        parser.add_argument("--min-confidence", type=float, default=0.5)
        parser.add_argument("--out", type=Path, help="Write the JSON report here.")
        parser.add_argument("--markdown", type=Path, help="Write a Markdown summary here.")
        parser.add_argument("--baseline", type=Path, help="Earlier JSON report to compare with.")
        parser.add_argument("--min-recall", type=float, help="Exit with an error below this recall.")
        parser.add_argument("--max-false-positives", type=int, help="Exit with an error above this count.")

    def handle(self, *args: Any, **options: Any) -> None:
        provider, model = self._provider(options)
        try:
            cases = evaluation.load_cases(options["cases"], options["case"] or None)
        except (evaluation.CaseError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        run_settings = {
            "profile": options["profile"],
            "extended_context": not options["no_context"],
            "min_confidence": options["min_confidence"],
        }
        results = []
        for case in cases:
            result = evaluation.run_case(
                case,
                provider,
                profile=options["profile"],
                extended_context=not options["no_context"],
                min_confidence=options["min_confidence"],
            )
            status = "ok" if not result.missed and not result.false_positives else "!!"
            self.stdout.write(
                f"{status} {case.name}: found {len(result.found)}/{result.expected_total}, "
                f"false positives {result.false_positives}, other {result.unexpected}"
            )
            results.append(result)
        report = evaluation.summarize(results, model=model, settings=run_settings)
        baseline = json.loads(options["baseline"].read_text()) if options["baseline"] else None
        summary = evaluation.markdown(report, baseline)
        self.stdout.write("\n" + summary)
        if options["out"]:
            evaluation.write_report(report, options["out"])
        if options["markdown"]:
            options["markdown"].write_text(summary)
        totals = report["totals"]
        if options["min_recall"] is not None and (totals["recall"] or 0) < options["min_recall"]:
            raise CommandError(f"Recall {totals['recall']} is below {options['min_recall']}")
        max_fp = options["max_false_positives"]
        if max_fp is not None and totals["false_positives"] > max_fp:
            raise CommandError(f"{totals['false_positives']} false positives (max {max_fp})")

    def _provider(self, options: dict[str, Any]) -> tuple[Any, str]:
        from apps.credentials.models import LLMCredential
        from apps.credentials.services import provider_for
        from apps.llm.registry import build_provider

        if options["credential"]:
            credential = LLMCredential.objects.filter(pk=options["credential"]).first()
            if credential is None or not credential.is_usable:
                raise CommandError("No valid LLM key with that id.")
            model = options["model"] or credential.default_model
            return provider_for(credential, model=model), model
        api_key = os.environ.get(options["api_key_env"], "")
        if options["provider"] != "fake" and not api_key:
            raise CommandError(f"Set {options['api_key_env']} to the provider's API key.")
        model = options["model"] or "demo"
        try:
            provider = build_provider(
                options["provider"], api_key=api_key, model=model, base_url=options["base_url"] or None
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        return provider, model
