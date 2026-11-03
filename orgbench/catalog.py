"""Model catalog: aliases -> provider, model id, pricing, rate limits."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict

from .paths import CACHE_DIR, DEFAULT_MODELS_FILE

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


class Price(BaseModel):
    """USD per million tokens."""

    model_config = ConfigDict(extra="forbid")
    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    long_context_threshold: int | None = None
    long_input: float | None = None
    long_output: float | None = None

    def rates(self, prompt_tokens: int) -> tuple[float, float, float, float]:
        """(input, output, cache_read, cache_write) per-token rates for a prompt of this size."""
        inp, out = self.input, self.output
        if self.long_context_threshold and prompt_tokens > self.long_context_threshold:
            inp = self.long_input if self.long_input is not None else inp
            out = self.long_output if self.long_output is not None else out
        scale = inp / self.input if self.input else 1.0
        cr = (self.cache_read if self.cache_read is not None else inp) * scale
        cw = (self.cache_write if self.cache_write is not None else inp) * scale
        return inp / 1e6, out / 1e6, cr / 1e6, cw / 1e6

    def worst_case_input_rate(self, prompt_tokens: int) -> float:
        inp, _, _, cw = self.rates(prompt_tokens)
        return max(inp, cw)


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = ""
    provider: Literal["anthropic", "openrouter", "mock"]
    model: str
    price: Price
    max_concurrency: int = 8
    rpm: int | None = None
    refusal_fallback: bool = False
    thinking: Literal["adaptive", "omit"] = "adaptive"
    thinking_display: Literal["summarized", "omitted"] = "summarized"
    effort_levels: list[str] = EFFORTS
    supports_reasoning: bool | None = None  # OpenRouter: None = unknown, send effort if asked
    notes: str = ""

    def map_effort(self, effort: str | None) -> str | None:
        """Map a requested effort onto the closest level the model accepts (rounding down)."""
        if effort is None:
            return None
        if effort in self.effort_levels:
            return effort
        idx = EFFORTS.index(effort)
        for lower in reversed(EFFORTS[: idx + 1]):
            if lower in self.effort_levels:
                return lower
        return self.effort_levels[0] if self.effort_levels else None

    def cost(self, usage) -> float:
        """Cost in USD of one call given a providers.base.Usage."""
        if usage.reported_cost is not None:
            return float(usage.reported_cost)
        prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        inp, out, cr, cw = self.price.rates(prompt)
        return (
            usage.input_tokens * inp
            + usage.cache_read_tokens * cr
            + usage.cache_write_tokens * cw
            + usage.output_tokens * out
        )


class Catalog:
    def __init__(self, models: dict[str, ModelSpec], *, offline: bool = False):
        self.models = models
        self.offline = offline
        self._or_cache: dict[str, dict] | None = None

    @classmethod
    def load(cls, path: str | Path | None = None, *, offline: bool = False) -> "Catalog":
        path = Path(path) if path else DEFAULT_MODELS_FILE
        data = yaml.safe_load(path.read_text()) or {}
        models = {}
        for alias, spec in (data.get("models") or {}).items():
            models[alias] = ModelSpec(alias=alias, **spec)
        return cls(models, offline=offline)

    def resolve(self, name: str) -> ModelSpec:
        if name in self.models:
            return self.models[name]
        if ":" in name:
            provider, model_id = name.split(":", 1)
            if provider == "anthropic":
                for spec in self.models.values():
                    if spec.provider == "anthropic" and spec.model == model_id:
                        return spec.model_copy(update={"alias": name})
                raise KeyError(
                    f"No price known for Anthropic model {model_id!r}. Add it to configs/models.yaml."
                )
            if provider == "openrouter":
                return self._resolve_openrouter(name, model_id)
            if provider == "mock":
                return ModelSpec(alias=name, provider="mock", model=model_id, price=Price(input=0, output=0))
        raise KeyError(
            f"Unknown model {name!r}. Use a catalog alias ({', '.join(sorted(self.models))}) "
            "or 'openrouter:<model-id>' / 'anthropic:<model-id>'."
        )

    # ---- OpenRouter live metadata ----
    def openrouter_models(self, refresh: bool = False) -> dict[str, dict]:
        if self._or_cache is not None and not refresh:
            return self._or_cache
        cache_file = CACHE_DIR / "openrouter_models.json"
        fresh = cache_file.exists() and time.time() - cache_file.stat().st_mtime < 86400
        if cache_file.exists() and (fresh or self.offline) and not refresh:
            data = json.loads(cache_file.read_text())
        else:
            if self.offline:
                raise RuntimeError("OpenRouter model list not cached and running offline.")
            resp = httpx.get(OPENROUTER_MODELS_URL, timeout=30)
            resp.raise_for_status()
            data = resp.json()["data"]
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data))
        self._or_cache = {m["id"]: m for m in data}
        return self._or_cache

    def _resolve_openrouter(self, name: str, model_id: str) -> ModelSpec:
        if self.offline:
            return ModelSpec(alias=name, provider="openrouter", model=model_id, price=Price(input=0, output=0))
        meta = self.openrouter_models().get(model_id)
        if meta is None:
            raise KeyError(f"OpenRouter has no model {model_id!r} (run `orgbench models refresh`).")
        params = meta.get("supported_parameters") or []
        if "tools" not in params:
            raise ValueError(f"OpenRouter model {model_id!r} does not support tool calling.")
        p = meta.get("pricing") or {}
        price = Price(
            input=float(p.get("prompt") or 0) * 1e6,
            output=float(p.get("completion") or 0) * 1e6,
            cache_read=float(p["input_cache_read"]) * 1e6 if p.get("input_cache_read") else None,
        )
        free = model_id.endswith(":free")
        return ModelSpec(
            alias=name,
            provider="openrouter",
            model=model_id,
            price=price,
            max_concurrency=2 if free else 16,
            rpm=16 if free else None,
            supports_reasoning="reasoning" in params,
        )
