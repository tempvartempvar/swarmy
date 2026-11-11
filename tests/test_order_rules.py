"""Structured orders, no verbatim relaying, curated wiki edits."""

import asyncio
import json


from swarmy.agents import build_tree
from swarmy.budget import RunBudget
from swarmy.catalog import Catalog
from swarmy.config import OrgConfig
from swarmy.providers import ProviderRegistry, ToolCall
from swarmy.runner import RunContext
from swarmy.tasks import make_task
from swarmy.tools import Toolbox, tool_specs
from swarmy.tracing import Tracer, Transcript

BRIEF_LINE = "GreenLoop is piloting an automatic irrigation controller for greenhouse Zone 3."


def _ctx(tmp_path, **org_extra):
    org = OrgConfig.model_validate({"name": "o", "levels": [
        {"role": "captain", "model": "mock", "span": 2}, {"role": "private", "model": "mock"}],
        "knowledge": {"wikis": [{"name": "orders", "scope": "org"}]}, **org_extra})
    root = build_tree(org, Catalog.load(offline=True))
    task = make_task("greenhouse", {}, 1)
    task.setup_workspace(tmp_path / "ws")
    ctx = RunContext(run_id="r", org=org, task=task, workspace=tmp_path / "ws", registry=ProviderRegistry(mock=True),
                     budget=RunBudget(1), tracer=Tracer(tmp_path / "db"), transcript=Transcript(tmp_path / "t"))
    ctx.tools = Toolbox(ctx)
    ctx.bind(root)
    root.current_order_text = "Assignment from the client:\n\n" + task.brief
    return ctx, root


def _call(ctx, agent, name, args):
    specs = {s.name: s for s in tool_specs(agent, ctx)}
    return asyncio.run(ctx.tools.execute(agent, ToolCall("c1", name, args), specs))


def test_delegate_schema_is_structured(tmp_path):
    ctx, root = _ctx(tmp_path)
    spec = next(s for s in tool_specs(root, ctx) if s.name == "delegate")
    item = spec.parameters["properties"]["assignments"]["items"]
    assert set(item["required"]) == {"to", "objective", "deliverables", "done_when"}
    assert {"context", "constraints"} <= set(item["properties"])


def test_rejects_relayed_orders_and_missing_fields(tmp_path):
    ctx, root = _ctx(tmp_path)
    relay = {"to": "private-1", "objective": "Do your part.", "deliverables": "A report.", "done_when": "Done.",
             "context": BRIEF_LINE + " " + ctx.task.brief[:900]}
    r = _call(ctx, root, "delegate", {"assignments": [relay]})
    assert r.is_error and "verbatim" in r.content
    r = _call(ctx, root, "delegate", {"assignments": [{"to": "private-1", "task": "everything"}]})
    assert r.is_error and "missing objective, deliverables, done_when" in r.content
    r = _call(ctx, root, "delegate", {"assignments": [{"to": "private-1", "objective": "x" * 900,
                                                        "deliverables": "y", "done_when": "z"}]})
    assert r.is_error and "under 500" in r.content




def test_wiki_edits_and_pasted_orders(tmp_path):
    ctx, root = _ctx(tmp_path)
    r = _call(ctx, root, "wiki_write", {"wiki": "orders", "page": "Draft", "content": "Pump: 10 l/min"})
    assert not r.is_error and ctx.wiki.find("orders", "Draft").current.summary == ""  # summary optional
    r = _call(ctx, root, "wiki_write", {"wiki": "orders", "page": "Brief", "summary": "copy",
                                        "content": ctx.task.brief[:1200]})
    assert r.is_error and "distilled" in r.content
    r = _call(ctx, root, "wiki_write", {"wiki": "orders", "page": "Facts", "summary": "Add pump flow from doc-004",
                                        "content": "- Pump flow: 10 l/min (doc-004)"})
    assert not r.is_error
    page = ctx.wiki.find("orders", "Facts")
    assert page.current.summary == "Add pump flow from doc-004"
    r = _call(ctx, root, "wiki_write", {"wiki": "orders", "page": "Facts", "summary": "again",
                                        "content": "- Pump flow: 10 l/min (doc-004)"})
    assert "No change" in r.content and len(page.revisions) == 1
    listing = _call(ctx, root, "wiki_read", {"wiki": "orders"}).content
    assert "last change: Add pump flow from doc-004" in listing


def test_mock_runs_comply_with_rules(tmp_path):
    from swarmy.runner import run_one

    org = OrgConfig.model_validate({"name": "o", "levels": [
        {"role": "captain", "model": "mock", "span": 2}, {"role": "private", "model": "mock"}],
        "knowledge": {"wikis": [{"name": "orders", "scope": "org"}]}})

    async def go():
        reg = ProviderRegistry(mock=True)
        try:
            return await run_one(org, make_task("greenhouse", {}, 1), catalog=Catalog.load(offline=True),
                                 registry=reg, tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=1)
        finally:
            await reg.aclose()

    s = asyncio.run(go())
    assert s["status"] == "completed" and s["mock"] is True
    t = Tracer(tmp_path / "db.sqlite")
    assert len(t.query("SELECT 1 FROM events WHERE kind='assign'")) == 2
    assert not t.query("SELECT 1 FROM events WHERE kind='tool' AND data_json LIKE '%\"ok\": false%'")


def test_failed_calls_are_logged_and_diagnosed(tmp_path):
    from swarmy.wiki import activity

    ctx, root = _ctx(tmp_path)
    r = _call(ctx, root, "wiki_write", {"wiki": "orders", "content": "no page title"})
    assert r.is_error
    act = activity(ctx.tracer, "r")
    assert act["writes"] == 0 and act["failed"] and "missing required argument 'page'" in act["failed"][0]["error"]


def test_submit_reminder_once(tmp_path):
    """An agent that can write a wiki but hasn't is reminded once, then its report is accepted."""
    from swarmy.providers import StepResult, Usage
    from swarmy.providers.mock_provider import MockProvider

    calls = []

    class Submitter(MockProvider):
        async def step(self, conv, spec, *, max_tokens, effort, tool_choice=None):
            calls.append(1)
            call = ToolCall(f"s{len(calls)}", "submit", {"report": "done", "status": "Found the pump flow"})
            conv.events.append(("assistant", [call]))
            return StepResult(text="", tool_calls=[call], usage=Usage(), stop_reason="tool_use", latency_s=0)

    ctx, root = _ctx(tmp_path)
    ctx.registry._providers["mock"] = Submitter()
    private = ctx.agents["private-1"]
    rep = asyncio.run(private.handle("Orders from captain: find the pump flow.", ctx, order_id=1))
    assert rep["status"] == "submitted" and len(calls) == 2
    assert ctx.tracer.query("SELECT 1 FROM events WHERE kind='wiki_reminder'")



def _scripted_submits(tmp_path, statuses, wikis=False):
    """A private submitting with each status in turn; returns (result, number of calls, tool results)."""
    from swarmy.providers import StepResult, Usage
    from swarmy.providers.mock_provider import MockProvider

    calls, results = [], []

    class Submitter(MockProvider):
        async def step(self, conv, spec, *, max_tokens, effort, tool_choice=None):
            if conv.events and conv.events[-1][0] == "tool":
                results.append(conv.events[-1][1][0].content)
            args = {"report": "done"}
            status = statuses[min(len(calls), len(statuses) - 1)]
            if status is not None:
                args["status"] = status
            calls.append(1)
            call = ToolCall(f"s{len(calls)}", "submit", args)
            conv.events.append(("assistant", [call]))
            return StepResult(text="", tool_calls=[call], usage=Usage(), stop_reason="tool_use", latency_s=0)

    extra = {} if wikis else {"knowledge": {"wikis": []}}
    ctx, root = _ctx(tmp_path, **extra)
    ctx.registry._providers["mock"] = Submitter()
    rep = asyncio.run(ctx.agents["private-1"].handle("Orders from captain: find the pump flow.", ctx, order_id=1))
    return rep, len(calls), results, ctx


def test_status_update_is_at_most_five_words(tmp_path):
    rep, n, _, ctx = _scripted_submits(tmp_path / "a", ["  Pump   flow found:  10 l/min "])
    assert n == 1 and rep["status_line"] == "Pump flow found: 10 l/min"
    ev = [json.loads(r["data_json"]) for r in ctx.tracer.query("SELECT data_json FROM events WHERE kind='agent_done'")]
    assert ev[-1]["status_line"] == "Pump flow found: 10 l/min"


def test_long_or_missing_status_is_rejected_once_then_trimmed(tmp_path):
    long = "I found the pump flow rate and recorded it on the wiki"
    rep, n, results, _ = _scripted_submits(tmp_path / "a", [long])
    assert n == 2 and "at most 5 words" in results[0] and "yours has 12 words" in results[0]
    assert rep["status"] == "submitted" and rep["status_line"] == "I found the pump flow"
    rep, n, results, _ = _scripted_submits(tmp_path / "b", [None, "Pump flow found"])
    assert n == 2 and "it is missing" in results[0] and rep["status_line"] == "Pump flow found"
    rep, n, _, _ = _scripted_submits(tmp_path / "c", [None])
    assert n == 2 and rep["status_line"] == "Done"


def test_turn_limit_gets_a_fallback_status(tmp_path):
    from swarmy.providers import StepResult, Usage
    from swarmy.providers.mock_provider import MockProvider

    class Reader(MockProvider):
        async def step(self, conv, spec, *, max_tokens, effort, tool_choice=None):
            call = ToolCall("r", "wiki_read", {"wiki": "orders"})
            conv.events.append(("assistant", [call]))
            return StepResult(text="", tool_calls=[call], usage=Usage(), stop_reason="tool_use", latency_s=0)

    ctx, root = _ctx(tmp_path, worker_max_turns=2)
    ctx.registry._providers["mock"] = Reader()
    rep = asyncio.run(ctx.agents["private-1"].handle("Orders from captain: read.", ctx, order_id=1))
    assert rep["status"] == "turn_limit" and rep["status_line"] == "Ran out of turns"
