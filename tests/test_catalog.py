import pytest

from agent_bridge import catalog as cat


def m(provider, mid, out, thinking=("low", "medium", "high"), status="fixed"):
    return {
        "provider": provider,
        "id": mid,
        "selector": f"{provider}/{mid}",
        "kind": "chat",
        "thinking": list(thinking) if thinking else None,
        "cost": {"input": out / 5, "output": out},
        "pricingStatus": status,
    }


CATALOG = [
    m("anthropic", "claude-haiku-4-5", 5),
    m("anthropic", "claude-haiku-5-5-20261001", 0.5),  # dated copy of the current haiku
    m("anthropic", "claude-haiku-5-5", 0.5),
    m("anthropic", "claude-sonnet-5-5", 10),
    m("proxy", "claude-sonnet-5-5", 11),  # same model through a proxy
    m("anthropic", "claude-opus-5-5", 20, ("low", "medium", "high", "xhigh", "max")),
    m("openai", "gpt-6-luna", 0.5),
    m("openai", "gpt-6.1-sol", 10),
    m("openai", "gpt-5.6-sol", 10),  # older version of the same family
    m("openai", "gpt-7-astra", 40, ("low", "xhigh")),
    m("local", "on-device", 0, None, "unknown"),  # free/local: never auto-picked
]


def selectors(tier_rows):
    return [r["model"] for r in tier_rows]


def all_tiered(tiers):
    return {r["model"] for rows in tiers.values() for r in rows}


def test_latest_version_wins_and_dated_copies_and_free_models_are_excluded():
    picked = all_tiered(cat.compute_tiers(CATALOG))
    assert "openai/gpt-6.1-sol" in picked and "openai/gpt-5.6-sol" not in picked
    assert "anthropic/claude-haiku-5-5" in picked and "anthropic/claude-haiku-4-5" not in picked
    assert not any("20261001" in s for s in picked)
    assert "local/on-device" not in picked


@pytest.mark.parametrize(
    "newer,older",
    [
        ("claude-sonnet-4-5@20250929", "claude-sonnet-4@20250514"),  # Vertex
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "us.anthropic.claude-sonnet-4-20250514-v1:0"),  # Bedrock
        ("gpt-5.1", "gpt-5-2025-08-07"),
        ("gpt-4o", "gpt-4o-2024-11-20"),  # same model: the undated id is kept
    ],
)
def test_build_dates_in_ids_are_not_versions(newer, older):
    for order in ([newer, older], [older, newer]):
        kept = cat.latest_per_family([m("p", i, 10) for i in order])
        assert [k["id"] for k in kept] == [newer]


def test_duplicate_model_across_providers_is_listed_once():
    picked = all_tiered(cat.compute_tiers(CATALOG))
    assert len({s for s in picked if s.endswith("claude-sonnet-5-5")}) == 1


def test_tiers_follow_price_and_include_every_vendor():
    tiers = cat.compute_tiers(CATALOG)
    assert set(selectors(tiers["fast"])) == {"anthropic/claude-haiku-5-5", "openai/gpt-6-luna"}
    assert "openai/gpt-7-astra" in selectors(tiers["frontier"])
    assert "openai/gpt-6.1-sol" in selectors(tiers["balanced"])


def test_no_vendor_is_favoured_without_preferences():
    frontier = cat.compute_tiers(CATALOG)["frontier"]
    assert frontier[0]["model"] == "openai/gpt-7-astra"  # most expensive, not the Anthropic one


def test_new_release_is_picked_up_automatically():
    newer = CATALOG + [m("anthropic", "claude-opus-6", 60)]
    frontier = cat.compute_tiers(newer)["frontier"]
    assert frontier[0]["model"] == "anthropic/claude-opus-6"
    assert "anthropic/claude-opus-5-5" not in all_tiered(cat.compute_tiers(newer))


def test_allowlist_limits_tiers_and_auto_resolution():
    prefs = cat.Policy(allowed=("openai/*",))
    assert all(s.startswith("openai/") for s in all_tiered(cat.compute_tiers(CATALOG, prefs)))
    model, _ = cat.resolve_auto("fast", CATALOG, prefs, "omp")
    assert model == "openai/gpt-6-luna"


def test_pin_overrides_tier_head_only_when_allowed():
    pinned = cat.Policy(pins={"frontier": "anthropic/claude-opus-5-5"})
    assert cat.compute_tiers(CATALOG, pinned)["frontier"][0]["model"] == "anthropic/claude-opus-5-5"
    blocked = cat.Policy(pins={"frontier": "anthropic/claude-opus-5-5"}, allowed=("openai/*",))
    assert cat.compute_tiers(CATALOG, blocked)["frontier"][0]["model"] != "anthropic/claude-opus-5-5"


def test_tier_thinking_is_clamped_to_what_the_model_supports():
    tiers = cat.compute_tiers(CATALOG)
    astra = next(r for r in tiers["frontier"] if r["model"] == "openai/gpt-7-astra")
    assert astra["thinking"] == "xhigh"  # wants "high"; supports only low/xhigh -> nearest


@pytest.mark.parametrize(
    "wanted,supported,expected",
    [("medium", ["low", "medium"], "medium"), ("max", ["low", "high"], "high"), ("high", None, None)],
)
def test_clamp_thinking(wanted, supported, expected):
    assert cat.clamp_thinking(wanted, supported) == expected


def test_claude_engine_tiers_respect_allowlist():
    assert cat.resolve_auto("frontier", [], cat.Policy(), "claude") == ("opus", "high")
    with pytest.raises(ValueError):
        cat.resolve_auto("frontier", [], cat.Policy(allowed=("claude-code/sonnet",)), "claude")


@pytest.mark.parametrize(
    "spec,tier",
    [("auto", "balanced"), ("auto:fast", "fast"), ("auto:frontier", "frontier"), ("automatic-1", None), ("acme/auto", None)],
)
def test_parse_auto(spec, tier):
    assert cat.parse_auto(spec) == tier


@pytest.mark.parametrize("spec", ["auto:turbo", "auto:"])
def test_unknown_tier_is_rejected(spec):
    with pytest.raises(ValueError):
        cat.parse_auto(spec)


def test_an_empty_catalog_gives_empty_tiers_and_a_clear_error():
    assert cat.compute_tiers([]) == {"fast": [], "balanced": [], "frontier": []}
    with pytest.raises(ValueError, match="no allowed model"):
        cat.resolve_auto("fast", [], cat.Policy(), "omp")


@pytest.mark.parametrize(
    "config",
    [
        {"allowed_models": "openai/*"},  # a string would otherwise be read as one pattern per character
        {"allowed_models": [""]},
        {"tiers": {"turbo": "x/y"}},
        {"tiers": ["x/y"]},
        {"tiers": {"fast": 3}},
    ],
)
def test_malformed_policies_are_rejected(config):
    with pytest.raises(ValueError):
        cat.Policy.from_config(config)
