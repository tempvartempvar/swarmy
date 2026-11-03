import asyncio

import pytest

from orgbench.budget import MIN_OUTPUT, RunBudget, RunBudgetExceeded, SweepBudget
from orgbench.catalog import Catalog


def test_run_budget_clamps_then_stops():
    opus = Catalog.load(offline=True).resolve("opus")
    b = RunBudget(0.30)
    assert b.plan_call(opus, 2000, 4000) == 4000
    clamped = b.plan_call(opus, 2000, 32000)  # $0.64 worst case does not fit
    assert MIN_OUTPUT <= clamped < 32000
    worst = 2000 * opus.price.worst_case_input_rate(2000) + clamped * opus.price.output / 1e6
    assert worst <= 0.30 + 1e-9
    b.charge(0.29)
    with pytest.raises(RunBudgetExceeded):
        b.plan_call(opus, 2000, 32000)


def test_free_model_never_clamped():
    free = Catalog.load(offline=True).resolve("gemma-free")
    assert RunBudget(0.0001).plan_call(free, 50_000, 8000) == 8000


def test_sweep_budget_reservations():
    async def go():
        sb = SweepBudget(1.0)
        assert await sb.reserve(0.6)
        assert not await sb.reserve(0.6)
        await sb.settle(0.6, 0.1)
        assert await sb.reserve(0.6)
        await sb.settle(0.6, 0.6)
        return sb
    sb = asyncio.run(go())
    assert sb.spent == pytest.approx(0.7) and sb.reserved == pytest.approx(0.0)
