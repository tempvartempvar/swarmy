"""Explicit trees, communication rules, wikis and messaging."""

import asyncio

import pytest
import yaml

from swarmy.catalog import Catalog
from swarmy.config import OrgConfig, load_org
from swarmy.paths import CONFIG_DIR
from swarmy.providers import ProviderRegistry
from swarmy.rules import all_contacts, relation, skipped_managers, wiki_instances
from swarmy.runner import run_one
from swarmy.tasks import make_task
from swarmy.tracing import Tracer

TREE = {
    "name": "uneven",
    "tree": {"role": "ceo", "model": "opus", "effort": "high", "children": [
        {"role": "cto", "name": "CTO", "model": "sonnet", "children": [
            {"role": "engineer", "model": "gpt-oss-120b"},
            {"role": "engineer", "model": "qwen-flash"},
            {"role": "engineer", "model": "gpt-oss-120b"},
        ]},
        {"role": "researcher", "model": "haiku"},
    ]},
}


def _levels_org(**extra):
    return OrgConfig.model_validate({"name": "u", "levels": [
        {"role": "ceo", "model": "opus", "span": 2},
        {"role": "director", "model": "sonnet", "span": 2},
        {"role": "manager", "model": "haiku", "span": 2},
        {"role": "worker", "model": "gpt-oss-120b"}], **extra})


def test_explicit_tree_normalizes():
    org = OrgConfig.model_validate(TREE)
    names = [n.name for n in org.nodes()]
    assert names == ["ceo", "CTO", "engineer-1.1", "engineer-1.2", "engineer-1.3", "researcher-2"]
    assert org.shape == "1-2-3" and org.n_workers == 4 and org.n_managers == 2
    assert not org.is_uniform()
    assert "engineer x3 [gpt-oss-120b x2, qwen-flash x1]" in org.describe()
    assert "tree" in org.compact_dict()
    again = OrgConfig.model_validate(yaml.safe_load(org.to_yaml()))
    assert [n.name for n in again.nodes()] == names


def test_uniform_tree_compacts_to_levels():
    org = _levels_org()
    as_tree = OrgConfig.model_validate({"name": "u", "tree": org.tree_dict()})
    assert as_tree.is_uniform()
    compact = as_tree.compact_dict()
    assert "levels" in compact and [lv.get("span", 0) for lv in compact["levels"]] == [2, 2, 2, 0]


def test_tree_overrides_by_role_top_bottom():
    org = OrgConfig.model_validate(TREE)
    new = org.with_overrides({"engineer": "haiku", "top": "sonnet"}, {"bottom": "low"})
    models = {n.name: n.settings.model for n in new.nodes()}
    assert models["ceo"] == "sonnet" and models["engineer-1.2"] == "haiku" and models["CTO"] == "sonnet"
    assert {n.settings.effort for n in new.nodes() if not n.children} == {"low"}
    with pytest.raises(KeyError):
        org.with_overrides({"intern": "haiku"})


def test_duplicate_names_rejected():
    bad = {"name": "x", "tree": {"role": "a", "model": "haiku", "children": [
        {"role": "b", "name": "dup", "model": "haiku"}, {"role": "b", "name": "dup", "model": "haiku"}]}}
    with pytest.raises(ValueError, match="unique"):
        OrgConfig.model_validate(bad)




def test_relations():
    org = _levels_org()
    n = {x.name: x for x in org.nodes()}
    w = n["worker-1.1.1"]
    assert relation(w, n["manager-1.1"]) == ("manager", 1)
    assert relation(w, n["worker-1.1.2"]) == ("peer", 0)
    assert relation(w, n["director-1"]) == ("skip_up", 2)
    assert relation(w, n["ceo"]) == ("skip_up", 3)
    assert relation(n["ceo"], w) == ("skip_down", 3)
    assert relation(w, n["worker-1.2.1"]) == ("cross_team", 0)
    assert relation(w, n["manager-2.1"]) == ("other", 0)
    assert [m.name for m in skipped_managers(w, n["ceo"])] == ["manager-1.1", "director-1"]


def test_contact_rules():
    assert all(not v for v in all_contacts(_levels_org()).values())  # strict tree by default
    org = _levels_org(communication={"chain": True, "peers": True, "skip_up": 1})
    c = {d["to"]: d["kind"] for d in all_contacts(org)["worker-1.1.1"]}
    assert c == {"manager-1.1": "manager", "worker-1.1.2": "peer", "director-1": "skip_up"}
    ceo = {d["to"] for d in all_contacts(org)["ceo"]}
    assert ceo == {"director-1", "director-2"}  # skip_down is 0
    org2 = _levels_org(communication={"skip_down": 2})
    assert len(all_contacts(org2)["ceo"]) == 4 + 8  # managers and workers, not direct reports (chain off)
    org3 = _levels_org(communication={"open": True})
    assert len(all_contacts(org3)["worker-1.1.1"]) == 14


def test_wiki_scopes_and_permissions():
    org = _levels_org(knowledge={"wikis": [
        {"name": "handbook", "scope": "org", "write": "managers"},
        {"name": "team", "scope": "team"},
        {"name": "dept", "scope": "department", "level": 1},
    ]})
    inst = {w.id: w for w in wiki_instances(org)}
    assert len([i for i in inst if i.startswith("team/")]) == 7  # one per manager
    assert len([i for i in inst if i.startswith("dept/")]) == 2
    hb = inst["handbook"]
    assert "worker-1.1.1" in hb.readers and "worker-1.1.1" not in hb.writers and "ceo" in hb.writers
    t = inst["team/manager-1.1"]
    assert set(t.members) == {"manager-1.1", "worker-1.1.1", "worker-1.1.2"}
    assert {"director-1", "ceo"} <= t.readers  # commanders above a wiki's scope can read it
    d = inst["dept/director-1"]
    assert "ceo" in d.readers and "ceo" not in d.writers and "worker-1.2.2" in d.writers


def _run(org, task, tmp_path):
    async def go():
        reg = ProviderRegistry(mock=True)
        events = []
        try:
            s = await run_one(org, task, catalog=Catalog.load(offline=True), registry=reg,
                              tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=1.0,
                              listener=events.append)
            return s, events
        finally:
            await reg.aclose()
    return asyncio.run(go())


def test_messaging_and_wikis_in_a_run(tmp_path):
    org = _levels_org(
        knowledge={"wikis": [{"name": "handbook", "scope": "org", "publish_brief": True},
                             {"name": "team", "scope": "team"}]},
        communication={"chain": True, "peers": True, "skip_up": 1, "cc_skipped": True, "max_messages": 3})
    s, live = _run(org, make_task("mixed", {}, 1), tmp_path)
    assert s["status"] == "completed", s.get("error")
    t = Tracer(tmp_path / "db.sqlite")
    kinds = {r["kind"] for r in t.query("SELECT DISTINCT kind FROM events")}
    assert {"ask", "message", "wiki_write", "wiki_read", "inbox"} <= kinds
    answers = t.query("SELECT COUNT(*) n FROM calls WHERE purpose='answer'")[0]["n"]
    assert answers > 0
    run_dir = tmp_path / "runs" / s["run_id"]
    assert (run_dir / "wiki" / "handbook" / "client-brief.md").exists()
    states = {e["state"] for e in live if e["type"] == "state"}
    assert {"working", "thinking", "waiting", "done", "answering"} <= states
    assert live[-1]["type"] == "finished"


def test_prompt_lists_wikis_and_contacts(tmp_path):
    from swarmy.agents import build_tree
    from swarmy.budget import RunBudget
    from swarmy.prompts import system_prompt
    from swarmy.runner import RunContext
    from swarmy.tools import tool_specs
    from swarmy.tracing import Transcript

    org = _levels_org(knowledge={"wikis": [{"name": "handbook", "write": "managers"}]},
                      communication={"chain": True, "skip_up": 1})
    root = build_tree(org, Catalog.load(offline=True))
    ctx = RunContext(run_id="r", org=org, task=make_task("qa", {}, 1), workspace=tmp_path,
                     registry=ProviderRegistry(mock=True), budget=RunBudget(1), tracer=Tracer(tmp_path / "db"),
                     transcript=Transcript(tmp_path / "t"))
    ctx.bind(root)
    worker = ctx.agents["worker-1.1.1"]
    prompt = system_prompt(worker, ctx)
    assert "`handbook` (whole organization; read only)" in prompt
    assert "director-1 (director): skip-level" in prompt and "manager-1.1 (manager): your manager" in prompt
    names = {s.name for s in tool_specs(worker, ctx)}
    assert {"ask", "send_message", "wiki_read", "wiki_search"} <= names and "wiki_write" not in names
    ask = next(s for s in tool_specs(worker, ctx) if s.name == "ask")
    assert set(ask.parameters["properties"]["to"]["enum"]) == {"manager-1.1", "director-1"}


def test_example_orgs_with_knowledge_load():
    for f in sorted((CONFIG_DIR / "orgs").glob("*.yaml")):
        load_org(f)


def test_runs_saved_by_older_versions_still_open(tmp_path):
    # Every run stores its org config. Options removed since then must not make old runs unreadable.
    import json

    from swarmy.analysis import _layer_roles
    from swarmy.config import org_from_json
    from swarmy.orders import load_orders

    org = load_org(CONFIG_DIR / "orgs" / "general-staff.yaml")
    old = json.loads(org.model_dump_json())
    old.update(memory="persistent", disclose_models=True)
    old["orders"].update(format="structured", blocking=False)
    old["knowledge"]["remind_before_submit"] = True
    old["knowledge"]["wikis"][0]["oversight"] = True
    old["communication"]["modes"] = ["ask", "message"]
    assert org_from_json(json.dumps(old)).shape == org.shape
    assert org_from_json(json.dumps({"name": "not an org"})) is None
    tracer = Tracer(tmp_path / "db")
    tracer.save_run({"run_id": "r1", "org_json": json.dumps(old), "status": "completed"})
    assert "field-marshal" in load_orders(tracer, "r1")["goals"]
    assert _layer_roles(json.dumps(old)) == org.layer_roles()
