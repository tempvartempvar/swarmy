import asyncio
import json

import pytest

from swarmy.catalog import Catalog
from swarmy.config import LevelSpec, OrgConfig
from swarmy.providers import ProviderRegistry
from swarmy.providers.base import FatalProviderError
from swarmy.runner import run_one
from swarmy.tasks import make_task
from swarmy.tracing import Tracer

from .stub_servers import (
    anthropic_stub,
    anthropic_tool_call,
    openrouter_stub,
    openrouter_tool_call,
)


@pytest.fixture
def local_env(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def _solo(model: str, effort=None) -> OrgConfig:
    return OrgConfig(name=f"solo-{model}", levels=[LevelSpec(role="solo", model=model, effort=effort)])


def test_anthropic_request_shape_and_cost(local_env, monkeypatch, tmp_path):
    task = make_task("qa", {"kind": "chain", "hops": 2}, 1)
    script = [
        anthropic_tool_call("search_docs", {"query": "manufacturer"}, model="claude-opus-5-5"),
        anthropic_tool_call("submit", {"answer": task.gold()["expected"], "report": "found it"},
                            model="claude-opus-5-5"),
    ]
    with anthropic_stub(script) as stub:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", stub.url)
        catalog = Catalog.load(offline=True)
        tracer = Tracer(tmp_path / "db.sqlite")

        async def go():
            reg = ProviderRegistry()
            try:
                return await run_one(_solo("opus", "max"), task, catalog=catalog, registry=reg, tracer=tracer,
                                     results_dir=tmp_path, run_cap=2.0)
            finally:
                await reg.aclose()

        summary = asyncio.run(go())

    assert summary["status"] == "completed", summary
    assert summary["passed"]
    first = stub.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert first["output_config"] == {"effort": "max"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["fallbacks"] == "default"
    assert first["stream"] is True
    assert "temperature" not in first
    assert {t["name"] for t in first["tools"]} == {"search_docs", "read_doc", "submit"}
    assert "server-side-fallback-2026-07-01" in stub.headers[0].get("anthropic-beta", "")
    # second request replays the assistant turn and sends the tool result in a user turn
    second = stub.requests[1]
    assert second["messages"][1]["role"] == "assistant"
    assert second["messages"][2]["content"][0]["type"] == "tool_result"
    # cost: 1200 in*$4 + 800 cache read*$0.2 + 300 cache write*$5 + 150 out*$20 (per M), twice
    per_call = (1200 * 4 + 800 * 0.2 + 300 * 5 + 150 * 20) / 1e6
    assert summary["cost_usd"] == pytest.approx(2 * per_call)


def test_openrouter_request_shape_retry_and_reported_cost(local_env, monkeypatch, tmp_path):
    task = make_task("qa", {"kind": "chain", "hops": 2}, 2)
    script = [
        (429, json.dumps({"error": {"message": "rate limited, slow down"}}).encode()),
        (200, openrouter_tool_call("submit", {"answer": task.gold()["expected"]}, cost=0.0003)),
    ]
    with openrouter_stub(script) as stub:
        monkeypatch.setenv("OPENROUTER_BASE_URL", stub.url + "/api/v1")
        async def no_sleep(_):
            return None

        monkeypatch.setattr("swarmy.providers.base.asyncio.sleep", no_sleep)
        catalog = Catalog.load(offline=True)
        tracer = Tracer(tmp_path / "db.sqlite")

        async def go():
            reg = ProviderRegistry()
            try:
                return await run_one(_solo("gpt-oss-120b", "high"), task, catalog=catalog, registry=reg,
                                     tracer=tracer, results_dir=tmp_path, run_cap=0.5)
            finally:
                await reg.aclose()

        summary = asyncio.run(go())

    assert summary["passed"], summary
    body = stub.requests[-1]
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["reasoning"] == {"effort": "high"}
    assert body["tool_choice"] == "auto"
    assert body["provider"] == {"require_parameters": True}
    assert body["messages"][0]["role"] == "system"
    assert summary["cost_usd"] == pytest.approx(0.0003)  # provider-reported cost wins
    calls = tracer.query("SELECT retries, rate_wait_s FROM calls")
    assert calls[0]["retries"] == 1 and calls[0]["rate_wait_s"] > 0


def test_openrouter_daily_quota_is_fatal(local_env, monkeypatch, tmp_path):
    task = make_task("qa", {"kind": "chain", "hops": 2}, 1)
    script = [(429, json.dumps({"error": {"message": "Rate limit exceeded: free-models-per-day"}}).encode())]
    with openrouter_stub(script) as stub:
        monkeypatch.setenv("OPENROUTER_BASE_URL", stub.url + "/api/v1")
        catalog = Catalog.load(offline=True)

        async def go():
            reg = ProviderRegistry()
            try:
                return await run_one(_solo("gemma-free"), task, catalog=catalog, registry=reg,
                                     tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=0.5)
            finally:
                await reg.aclose()

        summary = asyncio.run(go())
    assert summary["status"] == "fatal"
    assert isinstance(summary["fatal"], FatalProviderError)
