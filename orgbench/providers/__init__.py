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
    def __init__(self, mock: bool = False):
        self.mock = mock
        self._providers: dict[str, Provider] = {}
        self._gates: dict[str, RateGate] = {}

    def provider_for(self, spec: ModelSpec) -> Provider:
        kind = "mock" if self.mock else spec.provider
        if kind not in self._providers:
            if kind == "anthropic":
                from .anthropic_provider import AnthropicProvider

                self._providers[kind] = AnthropicProvider()
            elif kind == "openrouter":
                from .openrouter_provider import OpenRouterProvider

                self._providers[kind] = OpenRouterProvider()
            elif kind == "mock":
                from .mock_provider import MockProvider

                self._providers[kind] = MockProvider()
            else:
                raise ValueError(f"unknown provider {kind}")
        return self._providers[kind]

    def gate_for(self, spec: ModelSpec) -> RateGate:
        key = f"{spec.provider}:{spec.model}"
        if key not in self._gates:
            if self.mock:
                self._gates[key] = RateGate(64, None)
            else:
                self._gates[key] = RateGate(spec.max_concurrency, spec.rpm)
        return self._gates[key]

    async def aclose(self) -> None:
        for p in self._providers.values():
            await p.aclose()
