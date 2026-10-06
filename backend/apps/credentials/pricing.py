"""LLM pricing and cost calculation (KEY-05).

Reviewbot ships list prices for common hosted models. Providers change prices, so admins can edit them and
add models on the LLM keys page; self-hosted models can be priced at 0. Calls to models without a price are
recorded with an unknown cost (``cost_usd`` NULL) and reported as "unpriced".
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Q, Value

from apps.credentials.models import ModelPrice

PRICES_AS_OF = "2025-10"
MTOK = Decimal(1_000_000)
COST_PLACES = Decimal("0.000001")

# (provider, model prefix, USD per 1M input tokens, USD per 1M output tokens). Longest prefix wins.
DEFAULT_PRICES: list[tuple[str, str, str, str]] = [
    ("openai", "gpt-5-nano", "0.05", "0.40"),
    ("openai", "gpt-5-mini", "0.25", "2.00"),
    ("openai", "gpt-5", "1.25", "10.00"),
    ("openai", "gpt-4.1-nano", "0.10", "0.40"),
    ("openai", "gpt-4.1-mini", "0.40", "1.60"),
    ("openai", "gpt-4.1", "2.00", "8.00"),
    ("openai", "gpt-4o-mini", "0.15", "0.60"),
    ("openai", "gpt-4o", "2.50", "10.00"),
    ("openai", "o4-mini", "1.10", "4.40"),
    ("openai", "o3-mini", "1.10", "4.40"),
    ("openai", "o3", "2.00", "8.00"),
    ("anthropic", "claude-opus-4-5", "5.00", "25.00"),
    ("anthropic", "claude-opus-4", "15.00", "75.00"),
    ("anthropic", "claude-sonnet-4", "3.00", "15.00"),
    ("anthropic", "claude-3-7-sonnet", "3.00", "15.00"),
    ("anthropic", "claude-3-5-sonnet", "3.00", "15.00"),
    ("anthropic", "claude-haiku-4-5", "1.00", "5.00"),
    ("anthropic", "claude-3-5-haiku", "0.80", "4.00"),
    ("fake", "", "0", "0"),
]


def install_defaults(model: type[ModelPrice] = ModelPrice) -> int:
    """Adds missing default prices without touching rows an admin created or edited. Returns rows added."""
    added = 0
    for provider, prefix, inp, out in DEFAULT_PRICES:
        _, created = model.objects.get_or_create(
            provider=provider,
            model_prefix=prefix,
            defaults={
                "input_usd_per_mtok": Decimal(inp),
                "output_usd_per_mtok": Decimal(out),
                "is_default": True,
            },
        )
        added += int(created)
    return added


def best_match(prices: Iterable[ModelPrice], provider: str, model: str) -> ModelPrice | None:
    name = model.lower()
    candidates = [
        p for p in prices if p.provider in ("", provider) and name.startswith(p.model_prefix.lower())
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.provider == provider, len(p.model_prefix)))


def price_for(provider: str, model: str) -> ModelPrice | None:
    return best_match(ModelPrice.objects.filter(Q(provider="") | Q(provider=provider)), provider, model)


def cost(price: ModelPrice | None, input_tokens: int, output_tokens: int) -> Decimal | None:
    if price is None:
        return None
    total = (
        Decimal(input_tokens) * price.input_usd_per_mtok + Decimal(output_tokens) * price.output_usd_per_mtok
    ) / MTOK
    return total.quantize(COST_PLACES)


def cost_expression(price: ModelPrice) -> ExpressionWrapper:
    """The same calculation as ``cost`` as a database expression, for bulk updates."""
    return ExpressionWrapper(
        (
            F("input_tokens") * Value(price.input_usd_per_mtok)
            + F("output_tokens") * Value(price.output_usd_per_mtok)
        )
        / Value(MTOK),
        output_field=DecimalField(max_digits=12, decimal_places=6),
    )
