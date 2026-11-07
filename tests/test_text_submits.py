"""Models that write their submit call as text (gpt-oss does this sometimes) still get their work delivered."""

import asyncio
import json

import pytest

from swarmy.catalog import Catalog
from swarmy.config import LevelSpec, OrgConfig
from swarmy.providers import ProviderRegistry, StepResult, Usage
from swarmy.providers.mock_provider import MockProvider
from swarmy.runner import run_one
from swarmy.tasks import make_task
from swarmy.tracing import Tracer

from .stub_servers import openrouter_stub, openrouter_text, openrouter_tool_call
from .test_order_rules import _ctx
from .test_providers import local_env  # noqa: F401  (fixture)


def _texting(tmp_path, texts):
    """A private whose replies are the given texts (no tool calls); returns (result, prompts it saw, ctx)."""
    seen = []

    class Texter(MockProvider):
        async def step(self, conv, spec, *, max_tokens, effort, tool_choice=None):
            seen.append(conv.events[-1])
            text = texts[min(len(seen) - 1, len(texts) - 1)]
            return StepResult(text=text, tool_calls=[], usage=Usage(), stop_reason="end_turn", latency_s=0)

    ctx, root = _ctx(tmp_path, knowledge={"wikis": []})
    ctx.registry._providers["mock"] = Texter()
    rep = asyncio.run(ctx.agents["private-1"].handle("Orders from captain: find the pump flow.", ctx, order_id=1))
    events = [(r["kind"], json.loads(r["data_json"])) for r in ctx.tracer.query("SELECT kind, data_json FROM events")]
    return rep, seen, events


def test_submit_written_as_json_text_is_recovered(tmp_path):
    text = '{ "report": "Pump flow is 10 l/min (doc-004).", "status": "Pump flow found" }'
    rep, seen, events = _texting(tmp_path, [text])
    assert rep["status"] == "submitted" and rep["status_line"] == "Pump flow found"
    assert rep["report"] == "Pump flow is 10 l/min (doc-004)." and len(seen) == 1
    assert ("salvaged", {"tool": "submit", "accepted": True, "problem": ""}) in events


def test_named_tool_call_in_a_code_block_is_recovered(tmp_path):
    text = 'Done.\n```json\n{"name": "submit", "arguments": {"report": "All set.", "status": "Done and verified"}}\n```'
    rep, _, _ = _texting(tmp_path / "a", [text])
    assert rep["status"] == "submitted" and rep["report"] == "All set."
    text = '{"name": "submit", "arguments": "{\\"report\\": \\"As a string.\\", \\"status\\": \\"Done\\"}"}'
    rep, _, _ = _texting(tmp_path / "b", [text])
    assert rep["status"] == "submitted" and rep["report"] == "As a string."


def test_text_submit_without_status_gets_a_specific_reminder(tmp_path):
    rep, seen, events = _texting(tmp_path, ['{"report": "Pump flow is 10 l/min."}',
                                            '{"report": "Pump flow is 10 l/min.", "status": "Pump flow found"}'])
    assert rep["status"] == "submitted" and len(seen) == 2
    assert "written as text, so it was not delivered" in seen[1][1] and "at most 5 words" in seen[1][1]


def test_ordinary_text_is_not_mistaken_for_a_submit(tmp_path):
    rep, seen, events = _texting(tmp_path, ['Here is the config: {"pump": 10, "unit": "l/min"}', "Still thinking."])
    assert rep["status"] == "no_submit" and len(seen) == 3  # first try + two reminders
    assert not [e for e in events if e[0] == "salvaged"]


def _solo_gptoss():
    return OrgConfig(name="solo", levels=[LevelSpec(role="solo", model="gpt-oss-120b")])


def _run_stub(responder_script, tmp_path):
    task = make_task("qa", {"kind": "chain", "hops": 2}, 1)
    script = [s(task) if callable(s) else s for s in responder_script]
    with openrouter_stub(script) as stub:
        catalog = Catalog.load(offline=True)
        catalog.endpoints["openrouter"].base_url = stub.url + "/api/v1"

        async def go():
            reg = ProviderRegistry()
            try:
                return await run_one(_solo_gptoss(), task, catalog=catalog, registry=reg, preflight=False,
                                     tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=0.5)
            finally:
                await reg.aclose()
        return asyncio.run(go()), stub


def test_after_a_reminder_a_tool_call_is_required(local_env, tmp_path):  # noqa: F811
    submit = lambda task: (200, openrouter_tool_call("submit", {"answer": task.gold()["expected"], "status": "Found it"}))
    s, stub = _run_stub([(200, openrouter_text("I think I am done.")), submit], tmp_path)
    assert s["status"] == "completed" and s["passed"]
    assert [r["tool_choice"] for r in stub.requests] == ["auto", "required"]


def test_endpoint_refusing_required_falls_back(local_env, tmp_path):  # noqa: F811
    submit = lambda task: (200, openrouter_tool_call("submit", {"answer": task.gold()["expected"], "status": "Found it"}))
    refused = (400, json.dumps({"error": {"message": "tool_choice 'required' is not supported"}}).encode())
    s, stub = _run_stub([(200, openrouter_text("Done, I believe.")), refused, submit], tmp_path)
    assert s["status"] == "completed" and [r["tool_choice"] for r in stub.requests] == ["auto", "required", "auto"]
