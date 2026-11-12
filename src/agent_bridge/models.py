"""Which model an agent runs on: the live omp catalog, filtered by the user's policy."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import catalog as cat
from . import proc, store

OMP = proc.find_executable(
    "omp",
    os.environ.get("AGENT_BRIDGE_OMP"),
    Path.home() / "Library/Application Support/Tern/omp/bin/omp",
    Path.home() / ".bun/bin/omp",
    Path.home() / ".local/bin/omp",
    "/opt/homebrew/bin/omp",
    "/usr/local/bin/omp",
)
CATALOG_TTL = 600  # seconds before `omp models` is asked again
# `claude --model` takes aliases and full ids; anything else (a stray option, say) is refused.
_CLAUDE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]:-]*")


class Models:
    """The catalog (cached) and the policy: config.json, overridden by rules set during this run."""

    def __init__(self) -> None:
        self._session_rules: dict = {}
        self._catalog: list[dict] = []
        self._catalog_at = 0.0

    def rules(self) -> dict:
        """{"allowed_models": [...], "tiers": {...}} in force right now."""
        saved = store.read_config()
        return {
            "allowed_models": self._session_rules.get("allowed_models", saved.get("allowed_models", [])),
            "tiers": self._session_rules.get("tiers", saved.get("tiers", {})),
        }

    def policy(self) -> cat.Policy:
        return cat.Policy.from_config(self.rules())

    def set_rules(self, changes: dict) -> None:
        """Override rules until the server restarts. Rules that could never apply are refused."""
        cat.Policy.from_config(changes)
        known = {m["selector"] for m in self.catalog()}
        missing = sorted(set(changes.get("tiers", {}).values()) - known)
        if missing:
            raise ValueError(f"unknown model(s) {missing} in tiers (see list_models)")
        patterns = changes.get("allowed_models")
        if patterns and not any(cat.is_allowed(selector, patterns) for selector in known):
            raise ValueError(f"allowed_models {patterns} matches no model on this machine (see list_models)")
        self._session_rules.update(changes)

    def catalog(self, refresh: bool = False) -> list[dict]:
        """Chat models of every provider configured in omp on this machine."""
        if not OMP:
            raise RuntimeError("omp not found; install omp or set AGENT_BRIDGE_OMP")
        if refresh:
            proc.run([OMP, "models", "refresh"], check=False, timeout=180)
        if refresh or not self._catalog or time.monotonic() - self._catalog_at > CATALOG_TTL:
            listed = json.loads(proc.run([OMP, "models", "--json"], timeout=120)).get("models", [])
            self._catalog = [m for m in listed if m.get("kind", "chat") == "chat"]
            self._catalog_at = time.monotonic()
        return self._catalog

    def resolve(self, engine: str, model: str | None, thinking: str | None) -> tuple[str | None, str | None]:
        """Turn what the caller asked for into the exact (model, thinking) to launch, or raise.

        Expands auto tiers, checks the model exists and supports the thinking level, and enforces
        the allowlist. With an allowlist, "no model" means auto:balanced rather than the engine's
        own default, which the bridge could not check.
        """
        policy = self.policy()
        if model is None and policy.allowed:
            model = "auto"
        tier = cat.parse_auto(model) if model else None
        if tier:
            model, tier_thinking = cat.resolve_auto(tier, self.catalog() if engine == "omp" else [], policy, engine)
            thinking = thinking or tier_thinking
        if engine == "claude":
            self._check_claude(model, thinking)
        else:
            self._check_omp(model, thinking)
        self.require_allowed(engine, model)
        return model, thinking

    def require_allowed(self, engine: str, model: str | None) -> None:
        allowed = self.policy().allowed
        if model and not cat.is_allowed(cat.model_key(engine, model), allowed):
            raise ValueError(
                f"{cat.model_key(engine, model)} is not allowed by the model policy {list(allowed)}; "
                "pick an allowed model (list_models) or ask the user to change the policy"
            )

    @staticmethod
    def _check_claude(model: str | None, thinking: str | None) -> None:
        if model and not _CLAUDE_MODEL.fullmatch(model):
            raise ValueError(f"invalid Claude Code model {model!r}")
        if thinking and thinking not in (*cat.CLAUDE_EFFORT, "auto"):
            raise ValueError(f"Claude Code effort must be one of {', '.join(cat.CLAUDE_EFFORT)} or auto, got {thinking!r}")

    def _check_omp(self, model: str | None, thinking: str | None) -> None:
        if thinking and thinking not in (*cat.THINKING_ORDER, "auto"):
            raise ValueError(f"omp thinking must be one of {', '.join(cat.THINKING_ORDER)} or auto, got {thinking!r}")
        if not model:
            return
        entry = next((m for m in self.catalog() if m["selector"] == model), None)
        if not entry:
            close = [m["selector"] for m in self.catalog() if model.lower() in m["selector"].lower()][:10]
            raise ValueError(f"unknown model {model!r}; close matches: {close or 'none'} (see list_models)")
        levels = entry.get("thinking")
        if thinking and thinking not in ("off", "auto") and levels and thinking not in levels:
            raise ValueError(f"{model} supports thinking {levels}, not {thinking!r}")
