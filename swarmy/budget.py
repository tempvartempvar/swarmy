"""Spending guards.

RunBudget: before each call, the output cap (max_tokens) is clamped to what the run can still
afford assuming a worst case (whole prompt billed as a cache write, every output token used).
If it can't afford MIN_OUTPUT tokens, the run stops. So a run never exceeds its cap, apart from
small errors in the input-token estimate (which is deliberately conservative).

SweepBudget: each run reserves its cap before starting and releases the unused part when it
ends, so the sweep total stays under budget even with concurrent runs.
"""

from __future__ import annotations

import asyncio

from .catalog import ModelSpec

MIN_OUTPUT = 1024


class RunBudgetExceeded(Exception):
    pass


class RunBudget:
    def __init__(self, cap_usd: float | None):
        self.cap = cap_usd
        self.spent = 0.0

    def plan_call(self, spec: ModelSpec, est_input_tokens: int, desired_max_tokens: int) -> int:
        if self.cap is None:
            return desired_max_tokens
        remaining = self.cap - self.spent
        in_cost = est_input_tokens * spec.rate.worst_case_input_rate(est_input_tokens)
        _, out_rate, _, _ = spec.rate.rates(est_input_tokens)
        if out_rate == 0:
            if remaining - in_cost < 0:
                raise RunBudgetExceeded(f"run budget ${self.cap:.2f} exhausted")
            return desired_max_tokens
        affordable = int((remaining - in_cost) / out_rate)
        max_out = min(desired_max_tokens, affordable)
        if max_out < MIN_OUTPUT:
            raise RunBudgetExceeded(
                f"run budget ${self.cap:.2f} exhausted (spent ${self.spent:.4f}; next {spec.alias} call needs "
                f"~${in_cost + MIN_OUTPUT * out_rate:.4f})"
            )
        return max_out

    def charge(self, cost: float) -> None:
        self.spent += cost


class SweepBudget:
    def __init__(self, total_usd: float):
        self.total = total_usd
        self.spent = 0.0
        self.reserved = 0.0
        self._lock = asyncio.Lock()

    @property
    def available(self) -> float:
        return self.total - self.spent - self.reserved

    async def reserve(self, amount: float) -> bool:
        async with self._lock:
            if amount > self.available + 1e-9:
                return False
            self.reserved += amount
            return True

    async def settle(self, reserved: float, actual: float) -> None:
        async with self._lock:
            self.reserved -= reserved
            self.spent += actual
