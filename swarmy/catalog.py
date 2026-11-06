"""Endpoints and models.

An *endpoint* is somewhere models are served: the Anthropic API, OpenRouter, OpenAI, a local Ollama
or vLLM server, a company gateway... Two wire formats are supported: `anthropic` (Messages API) and
`openai` (chat completions, which almost every other server speaks). A *model* is an alias that
points at an endpoint plus a model id and a price.

Built-in endpoints are defined below; `configs/models.yaml` can add or override endpoints and
models, and `configs/models.local.yaml` (git-ignored, written by the designer's Models dialog)
overrides both. Any model can also be named inline as `<endpoint>:<model-id>`, e.g.
`ollama:qwen3:8b` or `openrouter:qwen/qwen3.7-flash`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .paths import CACHE_DIR, DEFAULT_MODELS_FILE

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = ""
    type: Literal["anthropic", "openai", "mock"]
    base_url: str | None = None  # None = the SDK default (anthropic) / required for openai
    api_key_env: str | None = None  # environment variable holding the key; None = no key needed
    headers: dict[str, str] = {}
    extra_body: dict = {}  # merged into every request body (openai type)
    tools_extra_body: dict = {}  # merged when tools are sent (openai type)
    effort_style: Literal["none", "openai", "openrouter"] = "none"  # how to send reasoning effort
    max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    live_pricing: bool = False  # prices can be read from <base_url>/models (OpenRouter style)
    max_concurrency: int = 8  # default per-model concurrency for models on this endpoint
    timeout_s: float = 600
    notes: str = ""
    source: str = Field("builtin", exclude=True)

    @property
    def key_present(self) -> bool:
        return self.api_key_env is None or bool(os.environ.get(self.api_key_env))

    @property
    def is_local(self) -> bool:
        if self.type == "mock":
            return True
        host = urlparse(self.base_url or "").hostname or ""
        return host in LOCAL_HOSTS or host.endswith(".local")


BUILTIN_ENDPOINTS: dict[str, dict] = {
    "anthropic": {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY",
                  "notes": "Claude via the Anthropic Messages API (streaming, adaptive thinking, caching)."},
    "openrouter": {"type": "openai", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY",
                   "headers": {"X-Title": "swarmy"}, "extra_body": {"usage": {"include": True}},
                   "tools_extra_body": {"provider": {"require_parameters": True}}, "effort_style": "openrouter",
                   "live_pricing": True, "max_concurrency": 16,
                   "notes": "Hundreds of models behind one key; reports the real cost of each call."},
    "openai": {"type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY",
               "effort_style": "openai", "max_tokens_param": "max_completion_tokens"},
    "ollama": {"type": "openai", "base_url": "http://localhost:11434/v1", "api_key_env": None, "max_concurrency": 2,
               "notes": "Local models via Ollama's OpenAI-compatible API. Pick models that support tool calling."},
    "mock": {"type": "mock", "notes": "Offline scripted models for testing the wiring."},
}


class Price(BaseModel):
    """USD per million tokens."""

    model_config = ConfigDict(extra="forbid")
    input: float = 0.0
    output: float = 0.0
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
    endpoint: str = ""
    model: str
    price: Price | None = None
    max_concurrency: int | None = None
    rpm: int | None = None
    refusal_fallback: bool = False  # anthropic endpoints only
    thinking: Literal["adaptive", "omit"] = "adaptive"  # anthropic endpoints only
    thinking_display: Literal["summarized", "omitted"] = "summarized"
    effort_levels: list[str] = EFFORTS
    supports_reasoning: bool | None = None  # openai-type endpoints: None = unknown, send effort if asked
    notes: str = ""
    source: str = Field("builtin", exclude=True)
    endpoint_cfg: Endpoint | None = Field(None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _legacy_provider(cls, data):
        """Older configs said `provider: anthropic|openrouter|mock`; that's now the endpoint name."""
        if isinstance(data, dict) and "provider" in data:
            data = dict(data)
            data.setdefault("endpoint", data.pop("provider"))
        return data

    @property
    def kind(self) -> str:
        return self.endpoint_cfg.type if self.endpoint_cfg else ("mock" if self.endpoint == "mock" else "openai")

    @property
    def provider(self) -> str:  # kept for logs and older callers: the endpoint name
        return self.endpoint

    @property
    def price_known(self) -> bool:
        return self.price is not None or (self.endpoint_cfg is not None and self.endpoint_cfg.is_local)

    @property
    def rate(self) -> Price:
        return self.price or Price()

    def concurrency(self) -> int:
        if self.max_concurrency:
            return self.max_concurrency
        return self.endpoint_cfg.max_concurrency if self.endpoint_cfg else 8

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
        inp, out, cr, cw = self.rate.rates(prompt)
        return (
            usage.input_tokens * inp
            + usage.cache_read_tokens * cr
            + usage.cache_write_tokens * cw
            + usage.output_tokens * out
        )


class Catalog:
    def __init__(self, models: dict[str, ModelSpec], endpoints: dict[str, Endpoint], *, offline: bool = False,
                 local_path: Path | None = None):
        self.models = models
        self.endpoints = endpoints
        self.offline = offline
        self.local_path = local_path
        self._live: dict[str, dict[str, dict]] = {}
        for spec in self.models.values():
            spec.endpoint_cfg = self.endpoints.get(spec.endpoint)

    @classmethod
    def load(cls, path: str | Path | None = None, *, offline: bool = False, local: bool = True) -> "Catalog":
        path = Path(path) if path else DEFAULT_MODELS_FILE
        endpoints = {n: Endpoint(name=n, source="builtin", **cfg) for n, cfg in BUILTIN_ENDPOINTS.items()}
        models: dict[str, ModelSpec] = {}
        layers = [(path, "models.yaml")]
        local_path = path.with_name("models.local.yaml")
        if local:
            layers.append((local_path, "local"))
        for file, source in layers:
            if not file.exists():
                continue
            data = yaml.safe_load(file.read_text()) or {}
            for name, cfg in (data.get("endpoints") or {}).items():
                endpoints[name] = Endpoint(name=name, source=source, **cfg)
            for alias, cfg in (data.get("models") or {}).items():
                models[alias] = ModelSpec(alias=alias, source=source, **cfg)
        unknown = sorted({s.endpoint for s in models.values() if s.endpoint not in endpoints})
        if unknown:
            raise ValueError(f"models refer to unknown endpoints: {unknown}")
        return cls(models, endpoints, offline=offline, local_path=local_path)

    # ---- local overrides (written by the designer) ----
    def local_data(self) -> dict:
        if self.local_path and self.local_path.exists():
            return yaml.safe_load(self.local_path.read_text()) or {}
        return {}

    def save_local(self, endpoints: dict, models: dict) -> None:
        """Validate and write configs/models.local.yaml (endpoint and model definitions only, never keys)."""
        for name, cfg in endpoints.items():
            Endpoint(name=name, **cfg)
            if any(k.lower() in ("api_key", "key", "token") for k in cfg):
                raise ValueError("put API keys in .env and reference them with api_key_env")
        known = set(self.endpoints) | set(endpoints)
        for alias, cfg in models.items():
            spec = ModelSpec(alias=alias, **cfg)
            if spec.endpoint not in known:
                raise ValueError(f"model {alias!r} uses unknown endpoint {spec.endpoint!r}")
        out = {"endpoints": endpoints, "models": models}
        header = ("# Local endpoints and models, written by the swarmy designer (Models dialog).\n"
                  "# Overrides configs/models.yaml. API keys belong in .env, referenced by api_key_env.\n")
        self.local_path.write_text(header + yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    # ---- resolution ----
    def resolve(self, name: str) -> ModelSpec:
        if name in self.models:
            return self.models[name]
        if ":" in name:
            ep_name, model_id = name.split(":", 1)
            ep = self.endpoints.get(ep_name)
            if ep is None:
                raise KeyError(f"unknown endpoint {ep_name!r} in {name!r} (endpoints: {', '.join(sorted(self.endpoints))})")
            spec = ModelSpec(alias=name, endpoint=ep_name, model=model_id, endpoint_cfg=ep, source="inline")
            if ep.type == "mock":
                spec.price = Price()
            elif ep.type == "anthropic":
                for other in self.models.values():
                    if other.kind == "anthropic" and other.model == model_id and other.price:
                        spec = other.model_copy(update={"alias": name, "endpoint": ep_name, "endpoint_cfg": ep})
                        break
            elif ep.live_pricing and not self.offline:
                spec = self._live_spec(spec, ep)
            return spec
        raise KeyError(
            f"Unknown model {name!r}. Use a catalog alias ({', '.join(sorted(self.models))}) "
            f"or '<endpoint>:<model-id>' with one of: {', '.join(sorted(self.endpoints))}."
        )

    def live_models(self, endpoint: str = "openrouter", refresh: bool = False) -> dict[str, dict]:
        """Model metadata from an OpenRouter-style <base_url>/models listing (cached for a day)."""
        if endpoint in self._live and not refresh:
            return self._live[endpoint]
        ep = self.endpoints[endpoint]
        cache_file = CACHE_DIR / f"{endpoint}_models.json"
        fresh = cache_file.exists() and time.time() - cache_file.stat().st_mtime < 86400
        if cache_file.exists() and (fresh or self.offline) and not refresh:
            data = json.loads(cache_file.read_text())
        else:
            if self.offline:
                raise RuntimeError(f"{endpoint} model list not cached and running offline.")
            resp = httpx.get(ep.base_url.rstrip("/") + "/models", timeout=30)
            resp.raise_for_status()
            data = resp.json()["data"]
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data))
        self._live[endpoint] = {m["id"]: m for m in data}
        return self._live[endpoint]

    # older name
    def openrouter_models(self, refresh: bool = False) -> dict[str, dict]:
        return self.live_models("openrouter", refresh)

    def _live_spec(self, spec: ModelSpec, ep: Endpoint) -> ModelSpec:
        meta = self.live_models(ep.name).get(spec.model)
        if meta is None:
            raise KeyError(f"{ep.name} has no model {spec.model!r} (run `swarmy models refresh`).")
        params = meta.get("supported_parameters") or []
        if "tools" not in params:
            raise ValueError(f"{ep.name} model {spec.model!r} does not support tool calling.")
        p = meta.get("pricing") or {}
        free = spec.model.endswith(":free")
        return spec.model_copy(update={
            "price": Price(input=float(p.get("prompt") or 0) * 1e6, output=float(p.get("completion") or 0) * 1e6,
                           cache_read=float(p["input_cache_read"]) * 1e6 if p.get("input_cache_read") else None),
            "max_concurrency": 2 if free else None, "rpm": 16 if free else None,
            "supports_reasoning": "reasoning" in params,
        })
