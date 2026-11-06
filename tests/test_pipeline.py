"""End-to-end with the offline mock provider: runs, sweeps, budgets, reports."""

import asyncio
import json

import yaml

from orgbench.analysis import write_report
from orgbench.catalog import Catalog
from orgbench.cli import main
from orgbench.config import LevelSpec, OrgConfig
from orgbench.paths import CONFIG_DIR
from orgbench.providers import ProviderRegistry, ToolCall
from orgbench.runner import run_one
from orgbench.sweep import load_sweep, plan_sweep, run_sweep
from orgbench.tasks import make_task
from orgbench.tools import Toolbox, tool_specs
from orgbench.tracing import Tracer


def _org(**kw):
    return OrgConfig(name="t", levels=[LevelSpec(role="ceo", model="opus", effort="max", span=2),
                                       LevelSpec(role="manager", model="haiku", span=2),
                                       LevelSpec(role="worker", model="gpt-oss-120b")], **kw)


def _run(org, task, tmp_path, cap=1.0, catalog=None):
    async def go():
        reg = ProviderRegistry(mock=True)
        try:
            return await run_one(org, task, catalog=catalog or Catalog.load(offline=True), registry=reg,
                                 tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=cap)
        finally:
            await reg.aclose()
    return asyncio.run(go())


def test_mock_run_records_hierarchy(tmp_path):
    s = _run(_org(communication="team_board"), make_task("mixed", {}, 1), tmp_path)
    assert s["status"] == "completed", s.get("error")
    assert set(s["nodes"]) == {"ceo", "manager-1", "manager-2", "worker-1.1", "worker-1.2", "worker-2.1",
                               "worker-2.2"}
    t = Tracer(tmp_path / "db.sqlite")
    kinds = {r["kind"] for r in t.query("SELECT DISTINCT kind FROM events")}
    assert {"assign", "report", "progress", "wiki_write"} <= kinds  # legacy team_board -> team wiki
    assigns = t.query("SELECT data_json FROM events WHERE kind='assign' AND level=1")
    assert all(json.loads(a["data_json"])["leaf"] for a in assigns)
    assert (tmp_path / "runs").exists()


def test_tool_permissions(tmp_path):
    from orgbench.agents import build_tree
    from orgbench.runner import RunContext
    from orgbench.tracing import Transcript
    from orgbench.budget import RunBudget

    task = make_task("mixed", {}, 1)
    org = _org()
    root = build_tree(org, Catalog.load(offline=True))
    ctx = RunContext(run_id="r", org=org, task=task, workspace=tmp_path, registry=ProviderRegistry(mock=True),
                     budget=RunBudget(1), tracer=Tracer(tmp_path / "db"), transcript=Transcript(tmp_path / "t"))
    ceo_tools = {s.name for s in tool_specs(root, ctx)}
    worker_tools = {s.name for s in tool_specs(root.children[0].children[0], ctx)}
    assert "delegate" in ceo_tools and "write_file" not in ceo_tools and "search_docs" not in ceo_tools
    assert {"read_file", "run_tests"} <= ceo_tools  # review tools
    assert {"write_file", "run_python", "search_docs", "read_doc"} <= worker_tools
    assert "delegate" not in worker_tools

    task.setup_workspace(tmp_path)
    ctx.tools = Toolbox(ctx)
    worker = root.children[0].children[0]
    specs = {s.name: s for s in tool_specs(worker, ctx)}
    run = lambda name, args: asyncio.run(ctx.tools.execute(worker, ToolCall("c", name, args), specs))
    assert run("write_file", {"path": "../escape.py", "content": "x"}).is_error
    assert run("write_file", {"path": "SPEC.md", "content": "x"}).is_error
    assert not run("write_file", {"path": "notes/a.txt", "content": "hi"}).is_error
    assert run("read_file", {"path": "notes/a.txt"}).content == "hi"
    assert run("read_file", {}).is_error  # missing argument
    out = run("run_python", {"code": "import os; print(sorted(k for k in os.environ if 'KEY' in k))"})
    assert "[]" in out.content  # API keys are not visible to model-run code
    assert "doc-" in run("search_docs", {"query": task.products[0]}).content


def test_budget_exhaustion_stops_run(tmp_path, monkeypatch):
    catalog = Catalog.load(offline=True)
    pricey = catalog.models["mock"].model_copy(update={"price": catalog.models["opus"].price})
    catalog.models["mock"] = pricey
    org = OrgConfig(name="pricey", levels=[LevelSpec(role="lead", model="mock", span=2),
                                           LevelSpec(role="worker", model="mock")])
    s = _run(org, make_task("code", {}, 1), tmp_path, cap=0.0005, catalog=catalog)
    assert s["status"] == "budget_exhausted"
    assert s["cost_usd"] <= 0.0005 + 1e-9


def test_mock_sweep_and_report(tmp_path):
    cfg_path = tmp_path / "sweep.yaml"
    cfg = yaml.safe_load((CONFIG_DIR / "sweeps" / "pilot.yaml").read_text())
    cfg["tasks"] = cfg["tasks"][:2]
    cfg_path.write_text(yaml.safe_dump(cfg))
    sweep_cfg, base = load_sweep(cfg_path)
    catalog = Catalog.load(offline=True)
    runs = plan_sweep(sweep_cfg, base, catalog)
    assert len(runs) == 8
    assert [r.org.name for r in runs[:4]] == ["mix-solo", "mix-d1", "mix-d2", "mix-d3"]  # interleaved
    sid = asyncio.run(run_sweep(sweep_cfg, base, catalog=catalog, mock=True, results_dir=tmp_path,
                                db_path=tmp_path / "db.sqlite", log=lambda *_: None))
    out = write_report(Tracer(tmp_path / "db.sqlite"), sid, tmp_path / "report")
    text = (out / "report.md").read_text()
    for name in ("mix-solo", "mix-d1", "mix-d2", "mix-d3"):
        assert name in text
    assert (out / "pareto.png").exists() and (out / "runs.csv").exists()


def test_all_example_sweeps_plan():
    catalog = Catalog.load(offline=True)
    for f in sorted((CONFIG_DIR / "sweeps").glob("*.yaml")):
        cfg, base = load_sweep(f)
        runs = plan_sweep(cfg, base, catalog)
        assert runs, f
        assert cfg.budget_usd <= 5.0


def test_cli_mock_run(tmp_path, capsys):
    code = main(["--results", str(tmp_path), "run", "--org", str(CONFIG_DIR / "orgs" / "corporate-5tier.yaml"),
                 "--task", "qa:kind=aggregate,n=4", "--mock", "--model", "worker=haiku", "--effort", "ceo=high"])
    out = capsys.readouterr().out
    assert code == 0
    assert "worker=haiku" in out and "worker-2.2.2.2" in out
