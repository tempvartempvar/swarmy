"""Provider-neutral types. Each provider keeps its conversation in its own native format."""

from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..catalog import ModelSpec


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema (type: object)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    error: str | None = None  # set when the model's arguments were not valid JSON


@dataclass
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0  # uncached input tokens
    output_tokens: int = 0  # includes thinking/reasoning tokens
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    reported_cost: float | None = None  # provider-reported USD (OpenRouter)

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class StepResult:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal | error | other
    latency_s: float
    rate_wait_s: float = 0.0
    retries: int = 0
    served_model: str = ""
    thinking: str = ""
    fallback: bool = False


PENDING_RESULT = "(still in progress - this is a side conversation, not the result)"


class ProviderError(Exception):
    """A call failed after retries. The agent reports the failure; the run continues."""


class FatalProviderError(Exception):
    """Auth failure, unknown model, exhausted daily quota... Stops the whole sweep."""


class Conversation(ABC):
    def __init__(self, system: str, tools: list[ToolSpec]):
        self.system = system
        self.tool_specs = tools
        self.chars = len(system) + sum(len(t.description) + len(str(t.parameters)) for t in tools)

    @abstractmethod
    def add_user(self, text: str) -> None: ...

    @abstractmethod
    def add_tool_results(self, results: list[ToolResult]) -> None: ...

    @abstractmethod
    def fork(self) -> "Conversation":
        """A copy for a side conversation (e.g. answering a colleague's question). Any tool calls
        still in flight get placeholder results so the copy can take a new user message."""

    def approx_input_tokens(self) -> int:
        # Conservative (over-)estimate used only for the budget guard.
        return int(self.chars / 3) + 200


class Provider(ABC):
    name: str = "base"

    @abstractmethod
    def new_conversation(self, system: str, tools: list[ToolSpec]) -> Conversation: ...

    @abstractmethod
    async def step(
        self, conv: Conversation, spec: ModelSpec, *, max_tokens: int, effort: str | None,
        tool_choice: str | None = None,
    ) -> StepResult: ...

    async def aclose(self) -> None:
        return None


class RateGate:
    """Per-model concurrency cap + optional requests-per-minute limit. Returns time waited."""

    def __init__(self, max_concurrency: int, rpm: int | None):
        self.sem = asyncio.Semaphore(max(1, max_concurrency))
        self.rpm = rpm
        self._times: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        t0 = time.monotonic()
        await self.sem.acquire()
        if self.rpm:
            async with self._lock:
                while True:
                    now = time.monotonic()
                    self._times = [t for t in self._times if now - t < 60]
                    if len(self._times) < self.rpm:
                        self._times.append(now)
                        break
                    await asyncio.sleep(60 - (now - self._times[0]) + 0.05)
        return time.monotonic() - t0

    def release(self) -> None:
        self.sem.release()


@dataclass
class RetryPolicy:
    max_retries: int = 6
    base_delay: float = 2.0
    max_delay: float = 60.0


@dataclass
class RetryOutcome:
    value: object = None
    wait_s: float = 0.0
    retries: int = 0
    errors: list[str] = field(default_factory=list)


class Retryable(Exception):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


async def with_retries(fn: Callable[[], Awaitable[object]], policy: RetryPolicy) -> RetryOutcome:
    """Call fn; on Retryable, back off (honouring retry-after) and try again."""
    out = RetryOutcome()
    for attempt in range(policy.max_retries + 1):
        try:
            out.value = await fn()
            return out
        except Retryable as e:
            out.errors.append(str(e))
            if attempt == policy.max_retries:
                raise ProviderError(f"gave up after {attempt + 1} attempts: {e}") from e
            delay = e.retry_after if e.retry_after else policy.base_delay * (2 ** attempt)
            delay = min(policy.max_delay, delay) + random.uniform(0, 1)
            out.retries += 1
            out.wait_s += delay
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")
