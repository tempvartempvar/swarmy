"""Provider registry: one client per provider, one rate gate per model."""

from __future__ import annotations

from ..catalog import ModelSpec
from .base import (
    Conversation,
    FatalProviderError,
    Provider,
    ProviderError,
    RateGate,
    StepResult,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)

__all__ = ["Conversation", "FatalProviderError", "Provider", "ProviderError", "ProviderRegistry", "RateGate",
           "StepResult", "ToolCall", "ToolResult", "ToolSpec", "Usage"]


class ProviderRegistry:
    """One provider client per endpoint, one rate gate per model."""

    def __init__(self, mock: bool = False):
        self.mock = mock
        self._providers: dict[str, Provider] = {}
        self._gates: dict[str, RateGate] = {}

    def provider_for(self, spec: ModelSpec) -> Provider:
        kind = "mock" if self.mock else spec.kind
        key = "mock" if kind == "mock" else spec.endpoint
        if key not in self._providers:
            if kind == "anthropic":
                from .anthropic_provider import AnthropicProvider

                self._providers[key] = AnthropicProvider(spec.endpoint_cfg)
            elif kind == "openai":
                from .openai_provider import OpenAICompatProvider

                if spec.endpoint_cfg is None:
                    raise FatalProviderError(f"model {spec.alias!r} has no endpoint configuration")
                self._providers[key] = OpenAICompatProvider(spec.endpoint_cfg)
            elif kind == "mock":
                from .mock_provider import MockProvider

                self._providers[key] = MockProvider()
            else:
                raise ValueError(f"unknown endpoint type {kind}")
        return self._providers[key]

    def gate_for(self, spec: ModelSpec) -> RateGate:
        key = f"{spec.endpoint}:{spec.model}"
        if key not in self._gates:
            self._gates[key] = RateGate(64, None) if self.mock else RateGate(spec.concurrency(), spec.rpm)
        return self._gates[key]

    async def aclose(self) -> None:
        for p in self._providers.values():
            await p.aclose()


# ---- checking models: one tiny call, made the way agents make it ----
_CHECK_TOOL = ToolSpec("noop", "Does nothing. Only here to check tool support.", {"type": "object", "properties": {}})
_CHECKED: dict[tuple[str, str], float] = {}  # (endpoint, model) -> when it last passed
CHECK_TTL_S = 1800


async def check_model(registry: "ProviderRegistry", spec: ModelSpec, timeout_s: float = 90) -> dict:
    """One tiny tool-enabled call. Catches a bad key, missing credits, an unknown model, no tool
    support, or privacy settings that exclude every provider. Returns ok, fatal, error, latency_s,
    text, served_model, input_tokens, output_tokens and cost_usd."""
    import asyncio
    import time

    t0 = time.monotonic()
    out = {"alias": spec.alias, "model": spec.model, "endpoint": spec.endpoint, "ok": False, "fatal": False,
           "error": None}
    try:
        provider = registry.provider_for(spec)
        conv = provider.new_conversation("Connectivity check.", [_CHECK_TOOL])
        conv.add_user("Reply with the single word OK.")
        async with asyncio.timeout(timeout_s):
            r = await provider.step(conv, spec, max_tokens=256, effort=None)
        out.update(ok=True, text=r.text[:200], served_model=r.served_model, input_tokens=r.usage.prompt_tokens,
                   output_tokens=r.usage.output_tokens, cost_usd=spec.cost(r.usage))
    except FatalProviderError as e:
        out.update(fatal=True, error=str(e))
    except Exception as e:  # provider errors and timeouts may be transient
        out["error"] = f"{type(e).__name__}: {e}"
    out["latency_s"] = round(time.monotonic() - t0, 2)
    return out


async def probe(spec: ModelSpec) -> dict:
    """check_model with its own client (the Models dialog and `swarmy models test`)."""
    reg = ProviderRegistry()
    try:
        return await check_model(reg, spec)
    finally:
        await reg.aclose()


async def preflight(registry: "ProviderRegistry", specs: list[ModelSpec]) -> list[dict]:
    """check_model for every distinct model of an org, in parallel, before a run. Models that passed in
    the last 30 minutes (and mock models) are skipped."""
    import asyncio
    import time

    async def one(spec: ModelSpec) -> dict:
        key = (spec.endpoint, spec.model)
        if spec.kind == "mock" or registry.mock or time.time() - _CHECKED.get(key, 0) < CHECK_TTL_S:
            return {"alias": spec.alias, "model": spec.model, "endpoint": spec.endpoint, "ok": True, "skipped": True}
        result = await check_model(registry, spec)
        if result["ok"]:
            _CHECKED[key] = time.time()
        return {**result, "skipped": False}

    unique = list({(s.endpoint, s.model): s for s in specs}.values())
    return list(await asyncio.gather(*(one(s) for s in unique)))
