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
                                     results_dir=tmp_path, run_cap=2.0, preflight=False)
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
        async def no_sleep(_):
            return None

        monkeypatch.setattr("swarmy.providers.base.asyncio.sleep", no_sleep)
        catalog = Catalog.load(offline=True)
        catalog.endpoints["openrouter"].base_url = stub.url + "/api/v1"
        tracer = Tracer(tmp_path / "db.sqlite")

        async def go():
            reg = ProviderRegistry()
            try:
                return await run_one(_solo("gpt-oss-120b", "high"), task, catalog=catalog, registry=reg,
                                     tracer=tracer, results_dir=tmp_path, run_cap=0.5, preflight=False)
            finally:
                await reg.aclose()

        summary = asyncio.run(go())

    assert summary["passed"], summary.get("error")
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
        catalog = Catalog.load(offline=True)
        catalog.endpoints["openrouter"].base_url = stub.url + "/api/v1"

        async def go():
            reg = ProviderRegistry()
            try:
                return await run_one(_solo("gemma-free"), task, catalog=catalog, registry=reg,
                                     tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=0.5, preflight=False)
            finally:
                await reg.aclose()

        summary = asyncio.run(go())
    assert summary["status"] == "fatal"
    assert isinstance(summary["fatal"], FatalProviderError)


def test_custom_openai_endpoint_without_key(local_env, tmp_path):
    """A self-hosted OpenAI-compatible server: no key, max_completion_tokens, reasoning_effort."""
    from swarmy.catalog import Endpoint, ModelSpec

    task = make_task("qa", {"kind": "chain", "hops": 2}, 3)
    script = [(200, openrouter_tool_call("submit", {"answer": task.gold()["expected"]}, cost=None))]
    with openrouter_stub(script) as stub:
        catalog = Catalog.load(offline=True)
        catalog.endpoints["mybox"] = Endpoint(name="mybox", type="openai", base_url=stub.url + "/v1",
                                              effort_style="openai", max_tokens_param="max_completion_tokens")
        catalog.models["local-llm"] = ModelSpec(alias="local-llm", endpoint="mybox", model="my-model",
                                                endpoint_cfg=catalog.endpoints["mybox"])

        async def go():
            reg = ProviderRegistry()
            try:
                return await run_one(_solo("local-llm", "high"), task, catalog=catalog, registry=reg,
                                     tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=0.5, preflight=False)
            finally:
                await reg.aclose()

        summary = asyncio.run(go())
    assert summary["passed"], summary.get("error")
    body, headers = stub.requests[0], stub.headers[0]
    assert body["model"] == "my-model" and "max_completion_tokens" in body and "max_tokens" not in body
    assert body["reasoning_effort"] == "high" and "reasoning" not in body and "usage" not in body
    assert "authorization" not in headers
    assert summary["cost_usd"] == 0  # no price set and no cost reported


def test_inline_endpoint_model_names():
    catalog = Catalog.load(offline=True)
    spec = catalog.resolve("ollama:qwen3:8b")
    assert spec.endpoint == "ollama" and spec.model == "qwen3:8b" and spec.kind == "openai"
    assert spec.price_known and spec.rate.input == 0  # local endpoints are free
    assert catalog.resolve("anthropic:claude-haiku-5-5").rate.input == 0.10
    with pytest.raises(KeyError):
        catalog.resolve("nowhere:model")


def test_anthropic_compatible_gateway(local_env, monkeypatch, tmp_path):
    from swarmy.catalog import Endpoint, ModelSpec

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("GATEWAY_KEY", "gw-secret")
    task = make_task("qa", {"kind": "chain", "hops": 2}, 1)
    with anthropic_stub([anthropic_tool_call("submit", {"answer": task.gold()["expected"]})]) as stub:
        catalog = Catalog.load(offline=True)
        catalog.endpoints["gw"] = Endpoint(name="gw", type="anthropic", base_url=stub.url, api_key_env="GATEWAY_KEY",
                                           headers={"X-Team": "research"})
        catalog.models["gw-haiku"] = ModelSpec(alias="gw-haiku", endpoint="gw", model="claude-haiku-5-5",
                                               endpoint_cfg=catalog.endpoints["gw"])

        async def go():
            reg = ProviderRegistry()
            try:
                return await run_one(_solo("gw-haiku"), task, catalog=catalog, registry=reg,
                                     tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=0.5, preflight=False)
            finally:
                await reg.aclose()

        summary = asyncio.run(go())
    assert summary["passed"], summary.get("error")
    auth = stub.headers[0].get("x-api-key") or stub.headers[0].get("authorization", "")
    assert "gw-secret" in auth and stub.headers[0]["x-team"] == "research"


ZDR_404 = json.dumps({"error": {"code": 404, "message": (
    "0 endpoints out of 1 requested are available matching your guardrail restrictions and data policy. "
    "ZDR violation (account settings): 1 endpoint excluded")}}).encode()


def test_preflight_stops_before_the_mission_when_a_model_cant_be_used(local_env, monkeypatch, tmp_path):
    import swarmy.providers as providers

    monkeypatch.setattr(providers, "_PREFLIGHT_OK", {})
    task = make_task("qa", {"kind": "chain", "hops": 2}, 1)
    with openrouter_stub([(404, ZDR_404)]) as stub:
        catalog = Catalog.load(offline=True)
        catalog.endpoints["openrouter"].base_url = stub.url + "/api/v1"

        async def go():
            reg = ProviderRegistry()
            try:
                return await run_one(_solo("qwen-flash"), task, catalog=catalog, registry=reg,
                                     tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=0.5)
            finally:
                await reg.aclose()

        s = asyncio.run(go())
    assert s["status"] == "fatal" and s["n_calls"] == 0 and len(stub.requests) == 1
    first = stub.requests[0]
    assert first["model"] == "qwen/qwen3.7-flash" and first["tools"][0]["function"]["name"] == "noop"
    assert s["end_reason"].startswith("Preflight: 1 of 1 models can't be used, so nothing was run.")
    assert "qwen-flash (qwen/qwen3.7-flash on openrouter), used by solo" in s["end_reason"]
    assert "openrouter.ai/settings/privacy" in s["end_reason"]
    log = (tmp_path / "runs" / s["run_id"] / "run.log").read_text()
    assert "preflight: qwen-flash (qwen/qwen3.7-flash on openrouter) can't be used" in log
    assert "mission ->" not in log


def test_preflight_passes_then_caches(local_env, monkeypatch, tmp_path):
    import swarmy.providers as providers

    monkeypatch.setattr(providers, "_PREFLIGHT_OK", {})
    task = make_task("qa", {"kind": "chain", "hops": 2}, 1)
    script = [(200, openrouter_tool_call("noop", {})),
              (200, openrouter_tool_call("submit", {"answer": task.gold()["expected"], "report": "done"}))]
    with openrouter_stub(script) as stub:
        catalog = Catalog.load(offline=True)
        catalog.endpoints["openrouter"].base_url = stub.url + "/api/v1"

        async def go(sub):
            reg = ProviderRegistry()
            try:
                return await run_one(_solo("gpt-oss-120b"), task, catalog=catalog, registry=reg,
                                     tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path / sub, run_cap=0.5)
            finally:
                await reg.aclose()

        s1 = asyncio.run(go("a"))
        n_after_first = len(stub.requests)
        s2 = asyncio.run(go("b"))
    assert s1["status"] == "completed" and s1["n_calls"] == 1 and n_after_first == 2  # preflight + one real call
    assert s2["status"] == "completed" and len(stub.requests) == 3  # checked recently: no second preflight
    log = (tmp_path / "a" / "runs" / s1["run_id"] / "run.log").read_text()
    assert "preflight: gpt-oss-120b (openai/gpt-oss-120b on openrouter) ok" in log
