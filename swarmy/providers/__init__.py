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


async def probe(spec: ModelSpec, prompt: str = "Reply with the single word OK.") -> dict:
    """One tiny real call to check an endpoint/model works. Returns text, latency, tokens and cost."""
    import time

    reg = ProviderRegistry()
    t0 = time.monotonic()
    try:
        provider = reg.provider_for(spec)
        conv = provider.new_conversation("You are a connectivity check. Follow the instruction exactly.", [])
        conv.add_user(prompt)
        r = await provider.step(conv, spec, max_tokens=512, effort="low" if spec.kind == "anthropic" else None)
        return {"ok": True, "text": r.text[:200], "latency_s": round(time.monotonic() - t0, 2),
                "served_model": r.served_model, "input_tokens": r.usage.prompt_tokens,
                "output_tokens": r.usage.output_tokens, "cost_usd": spec.cost(r.usage)}
    except Exception as e:  # report any failure to the caller
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "latency_s": round(time.monotonic() - t0, 2)}
    finally:
        await reg.aclose()
