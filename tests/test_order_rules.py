"""Structured orders, no verbatim relaying, curated wiki edits."""

import asyncio

import pytest

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
    assert r.is_error and "structured order" in r.content
    r = _call(ctx, root, "delegate", {"assignments": [{"to": "private-1", "objective": "x" * 900,
                                                        "deliverables": "y", "done_when": "z"}]})
    assert r.is_error and "under 500" in r.content


def test_free_format_and_forwarding_toggle(tmp_path):
    ctx, root = _ctx(tmp_path, orders={"format": "free", "no_forwarding": False})
    spec = next(s for s in tool_specs(root, ctx) if s.name == "delegate")
    assert spec.parameters["properties"]["assignments"]["items"]["required"] == ["to", "task"]
    order, fields = ctx.tools._order_from(root, {"to": "private-1", "task": ctx.task.brief[:1500]})
    assert order.startswith("GreenLoop") and fields == {}


def test_wiki_edit_needs_summary_and_rejects_pasted_orders(tmp_path):
    ctx, root = _ctx(tmp_path)
    r = _call(ctx, root, "wiki_write", {"wiki": "orders", "page": "Facts", "content": "Pump: 10 l/min"})
    assert r.is_error and "summary" in r.content
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


@pytest.mark.parametrize("fmt", ["structured", "free"])
def test_mock_runs_comply_with_rules(tmp_path, fmt):
    from swarmy.runner import run_one

    org = OrgConfig.model_validate({"name": "o", "levels": [
        {"role": "captain", "model": "mock", "span": 2}, {"role": "private", "model": "mock"}],
        "orders": {"format": fmt}, "knowledge": {"wikis": [{"name": "orders", "scope": "org"}]}})

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
