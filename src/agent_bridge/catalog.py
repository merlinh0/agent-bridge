"""Model catalog and automatic tiers.

Nothing here is hard-coded to a model name or a vendor: tiers are recomputed from the live omp
catalog (`omp models --json`, i.e. every model the providers configured on this machine expose),
so a newly released model is picked up as soon as omp lists it.

How a tier is built (pure functions, see tests/test_catalog.py):
1. Keep chat models with a known, non-zero output price that the allowlist permits.
   Free/local models stay selectable by name.
2. Group by family (the model id without its version numbers and build date, across providers)
   and keep the newest version of each family, preferring an undated id over its dated copies.
3. Rank the distinct output prices and split them in thirds: fast / balanced / frontier.
A model pinned to a tier by the policy goes first in that tier.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

TIERS = ("fast", "balanced", "frontier")
THINKING_ORDER = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
TIER_THINKING = {"fast": "low", "balanced": "medium", "frontier": "high"}
# Claude Code resolves these aliases to the latest model of each family itself.
CLAUDE_TIERS = {"fast": "haiku", "balanced": "sonnet", "frontier": "opus"}
CLAUDE_EFFORT = ("low", "medium", "high", "xhigh", "max")

# Build dates inside ids (-20251001, @20250929, -2025-08-07) are not version numbers.
_DATE = re.compile(r"[-@](?:20\d{6}|20\d{2}-\d{2}-\d{2})")
_NUMBER = re.compile(r"\d+")


@dataclass(frozen=True)
class Policy:
    """The user's model rules."""

    allowed: tuple[str, ...] = ()  # glob patterns on model keys; empty = every available model
    pins: dict[str, str] = field(default_factory=dict)  # tier -> model selector

    @classmethod
    def from_config(cls, config: dict) -> Policy:
        """Build from {"allowed_models": [...], "tiers": {...}}, rejecting anything malformed."""
        allowed = config.get("allowed_models", [])
        if not isinstance(allowed, list) or not all(isinstance(p, str) and p for p in allowed):
            raise ValueError("allowed_models must be a list of non-empty strings")
        pins = config.get("tiers", {})
        if not isinstance(pins, dict) or not all(isinstance(m, str) and m for m in pins.values()):
            raise ValueError("tiers must map a tier name to a model")
        unknown = sorted(set(pins) - set(TIERS))
        if unknown:
            raise ValueError(f"unknown tier(s) {unknown}; tiers are {', '.join(TIERS)}")
        return cls(allowed=tuple(allowed), pins=dict(pins))


def model_key(engine: str, model: str) -> str:
    """What the allowlist is matched against: an omp selector as it is ("openai-codex/gpt-6.1-sol"),
    a Claude Code model as "claude-code/<model>"."""
    return model if engine == "omp" else f"claude-code/{model}"


def is_allowed(key: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(key.lower(), p.lower()) for p in patterns)


def version_key(model_id: str) -> tuple[int, ...]:
    """Numbers of the id in order, so (6, 1) for "gpt-6.1-sol" sorts after (5, 6) for "gpt-5.6-sol"."""
    return tuple(int(n) for n in _NUMBER.findall(_DATE.sub("", model_id)))


def family_key(model_id: str) -> str:
    """The id without its version numbers and build date: what stays the same from one release to the next."""
    base = _NUMBER.sub("", _DATE.sub("", model_id))
    return re.sub(r"[-._@:]+", "-", base).strip("-")


def output_price(model: dict) -> float:
    return float((model.get("cost") or {}).get("output") or 0)


def latest_per_family(models: list[dict]) -> list[dict]:
    """One model per family: the newest version, and an undated id rather than its dated copy.

    The same model offered by two providers is kept once (the first provider alphabetically).
    """
    families: dict[str, list[dict]] = {}
    for model in sorted(models, key=lambda m: m["selector"]):
        families.setdefault(family_key(model["id"]), []).append(model)
    return [max(family, key=lambda m: (version_key(m["id"]), not _DATE.search(m["id"]))) for family in families.values()]


def clamp_thinking(wanted: str, supported: list[str] | None) -> str | None:
    """`wanted`, or the closest level the model supports; None when the model has no levels."""
    if not supported:
        return None
    if wanted in supported:
        return wanted
    ranked = [level for level in supported if level in THINKING_ORDER]
    if not ranked:
        return supported[0]
    return min(ranked, key=lambda level: abs(THINKING_ORDER.index(level) - THINKING_ORDER.index(wanted)))


def _split_by_price(models: list[dict]) -> dict[str, list[dict]]:
    """Cheapest third of the distinct prices -> fast, priciest third -> frontier, the rest -> balanced."""
    prices = sorted({output_price(m) for m in models})
    edge = max(1, len(prices) // 3)
    tiers: dict[str, list[dict]] = {tier: [] for tier in TIERS}
    for model in models:
        rank = prices.index(output_price(model))
        tiers["fast" if rank < edge else "frontier" if rank >= len(prices) - edge else "balanced"].append(model)
    # With fewer than three price points a tier is empty: it borrows its neighbour's models.
    for tier, neighbours in (("balanced", ("frontier", "fast")), ("frontier", ("balanced", "fast"))):
        if not tiers[tier]:
            tiers[tier] = list(next((tiers[n] for n in neighbours if tiers[n]), []))
    return tiers


def compute_tiers(catalog: list[dict], policy: Policy | None = None, per_tier: int = 3) -> dict[str, list[dict]]:
    """Tier -> candidates [{model, thinking, usd_per_mtok_in_out}], most capable (priciest) first.

    The first candidate of a tier is what `auto:<tier>` resolves to.
    """
    policy = policy or Policy()
    by_selector = {m["selector"]: m for m in catalog}
    priced = [m for m in catalog if output_price(m) > 0 and is_allowed(m["selector"], policy.allowed)]
    tiers = {}
    for tier, candidates in _split_by_price(latest_per_family(priced)).items():
        candidates.sort(key=lambda m: (-output_price(m), m["selector"]))
        pinned = policy.pins.get(tier)
        if pinned in by_selector and is_allowed(pinned, policy.allowed):
            candidates = [by_selector[pinned]] + [m for m in candidates if m["selector"] != pinned]
        tiers[tier] = [
            {
                "model": m["selector"],
                "thinking": clamp_thinking(TIER_THINKING[tier], m.get("thinking")),
                "usd_per_mtok_in_out": [(m.get("cost") or {}).get("input"), output_price(m)],
            }
            for m in candidates[:per_tier]
        ]
    return tiers


def parse_auto(spec: str) -> str | None:
    """Tier named by 'auto' (balanced) or 'auto:<tier>'; None when `spec` is an explicit model."""
    if spec == "auto":
        return "balanced"
    if not spec.startswith("auto:"):
        return None
    tier = spec.removeprefix("auto:")
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; use one of {', '.join('auto:' + t for t in TIERS)}")
    return tier


def resolve_auto(tier: str, catalog: list[dict], policy: Policy, engine: str) -> tuple[str, str | None]:
    """(model, thinking) that `tier` currently stands for on the given engine."""
    if engine == "claude":
        alias = CLAUDE_TIERS[tier]
        if not is_allowed(model_key("claude", alias), policy.allowed):
            raise ValueError(f"claude-code/{alias} (tier {tier}) is not in the allowed models {list(policy.allowed)}")
        return alias, TIER_THINKING[tier]
    candidates = compute_tiers(catalog, policy, per_tier=1)[tier]
    if not candidates:
        raise ValueError(f"no allowed model for tier {tier!r}; see list_models and get_model_policy")
    return candidates[0]["model"], candidates[0]["thinking"]
