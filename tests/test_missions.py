"""Missions, goals, forces and the chain of orders."""

import asyncio

from swarmy.catalog import Catalog
from swarmy.cli import main
from swarmy.config import load_org
from swarmy.orders import load_orders, render
from swarmy.paths import CONFIG_DIR
from swarmy.providers import ProviderRegistry
from swarmy.runner import run_one
from swarmy.tasks import make_task
from swarmy.tracing import Tracer

MISSION = "Build a simulation of orbital dynamics for satellites in LEO, MEO, GEO and a Molniya orbit."


def test_mission_task():
    t = make_task("mission", {"text": MISSION})
    assert t.family == "mission" and t.task_id.startswith("mission/build-a-simulation-of-orbital")
    assert t.has_workspace and not t.answer_required and MISSION in t.brief


def test_mission_run_records_chain_of_orders(tmp_path):
    org = load_org(CONFIG_DIR / "orgs" / "general-staff.yaml")

    async def go():
        reg = ProviderRegistry(mock=True)
        try:
            return await run_one(org, make_task("mission", {"text": MISSION}), catalog=Catalog.load(offline=True),
                                 registry=reg, tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=1)
        finally:
            await reg.aclose()

    s = asyncio.run(go())
    assert s["status"] == "completed" and s["score"] is None
    data = load_orders(Tracer(tmp_path / "db.sqlite"), s["run_id"])
    orders = {o["id"]: o for o in data["orders"]}
    assert orders[0]["to"] == "field-marshal" and orders[0]["report"]["status"] == "submitted"
    # every order links to the order its issuer was working on
    for o in data["orders"][1:]:
        issuer_orders = [p for p in data["orders"] if p["to"] == o["from"]]
        assert o["parent"] in {p["id"] for p in issuer_orders}
    scouts = [o for o in data["orders"] if o["to"].startswith("scout")]
    assert len(scouts) == 3 and all(orders[o["parent"]]["to"] == "intelligence-colonel" for o in scouts)
    text = render(data)
    assert "[0] client → field-marshal" in text and "goal:" in text and "intelligence-colonel → scout-1.1" in text


def test_prompts_have_goals_and_forces(tmp_path):
    from swarmy.agents import build_tree
    from swarmy.budget import RunBudget
    from swarmy.prompts import system_prompt
    from swarmy.runner import RunContext
    from swarmy.tracing import Transcript

    org = load_org(CONFIG_DIR / "orgs" / "general-staff.yaml")
    root = build_tree(org, Catalog.load(offline=True))
    ctx = RunContext(run_id="r", org=org, task=make_task("mission", {"text": MISSION}), workspace=tmp_path,
                     registry=ProviderRegistry(mock=True), budget=RunBudget(1), tracer=Tracer(tmp_path / "db"),
                     transcript=Transcript(tmp_path / "t"))
    ctx.bind(root)
    fm = system_prompt(root, ctx)
    assert "## Your standing goal" in fm and "split work so intelligence and engineering" in fm
    assert "## Your forces" in fm
    assert "intelligence-colonel (colonel): model claude-haiku-5-5; commands 3 positions below, 3 doing hands-on" in fm
    assert "Standing goal: Every fact the engineers need" in fm
    scout = system_prompt(ctx.agents["scout-1.1"], ctx)
    assert "## Your forces" not in scout and "## Your standing goal" not in scout


def test_cli_mission_and_orders(tmp_path, capsys):
    assert main(["--results", str(tmp_path), "run", "--org", str(CONFIG_DIR / "orgs" / "regiment.yaml"),
                 "--mission", MISSION, "--mock"]) == 0
    out = capsys.readouterr().out
    assert "score not graded" in out and "workspace:" in out
    assert main(["--results", str(tmp_path), "orders"]) == 0
    out = capsys.readouterr().out
    assert "[0] client → colonel" in out and "colonel → captain-1" in out
