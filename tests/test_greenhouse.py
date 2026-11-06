"""The greenhouse exercise: solvable, gradeable, and hard to pass by accident."""

import asyncio

import pytest

from swarmy.catalog import Catalog
from swarmy.config import load_org
from swarmy.paths import CONFIG_DIR
from swarmy.providers import ProviderRegistry
from swarmy.runner import run_one
from swarmy.tasks import make_task
from swarmy.tracing import Tracer

NAIVE = {
    "never waters": "def decide(obs):\n    return 0\n",
    "floods by day": "def decide(obs):\n    return 0 if (obs['hour'] >= 21 or obs['hour'] < 7) else 10000\n",
    "ignores quiet hours": "def decide(obs):\n    return 120.0\n",
}


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_reference_passes_stub_and_naive_fail(tmp_path, seed):
    t = make_task("greenhouse", {}, seed)
    ws = tmp_path / "ws"
    t.setup_workspace(ws)
    assert t.grade(None, ws).score == 0.0
    for rel, src in t.reference_files().items():
        (ws / rel).write_text(src)
    g = t.grade(None, ws)
    assert g.score == 1.0 and g.passed and len(g.details["scenarios_passed"]) == 6
    for name, src in NAIVE.items():
        (ws / "controller.py").write_text(src)
        assert t.grade(None, ws).details["scenarios_passed"] == [], name
    assert 0.6 <= t.p["required_in_band"] <= 0.9


def test_public_tests_report_each_criterion(tmp_path):
    t = make_task("greenhouse", {}, 1)
    ws = tmp_path / "ws"
    t.setup_workspace(ws)
    report = t.public_tests(ws)
    assert "sim.py: 0/4" in report and "ERROR official-mild" in report
    (ws / "sim.py").write_text(t.ref_sim)
    (ws / "controller.py").write_text("def decide(obs):\n    return 0\n")
    report = t.public_tests(ws)
    assert "sim.py: 4/4" in report and "FAIL official-mild" in report and "[FAIL]" in report


def test_knowledge_base_has_facts_and_traps():
    t = make_task("greenhouse", {}, 4)
    titles = [d.title for d in t.corpus.docs.values()]
    assert {"Zone 3 layout", "Zone 1 layout", "Soil physics notes (2024 revision)", "Soil physics notes (2019)",
            "Operating rules", "GreenLoop pilot: client requirements"} <= set(titles)
    text = " ".join(d.text for d in t.corpus.docs.values())
    assert f"grows {t.p['crop']}" in text and f"{t.p['budget_l']} liters" in text
    assert t.corpus.search("Zone 3 pump", k=3)[0][0].title in ("Zone 3 layout", "Memo: Zone 3 pump upgrade")


def test_openrouter_squad_runs_the_exercise_offline(tmp_path):
    org = load_org(CONFIG_DIR / "orgs" / "openrouter-squad.yaml")
    catalog = Catalog.load(offline=True)
    assert {catalog.resolve(n.settings.model).endpoint for n in org.nodes()} == {"openrouter"}

    async def go():
        reg = ProviderRegistry(mock=True)
        try:
            return await run_one(org, make_task("greenhouse", {}, 1), catalog=catalog, registry=reg,
                                 tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=1.0)
        finally:
            await reg.aclose()

    s = asyncio.run(go())
    assert s["status"] == "completed" and s["score"] == 0.0  # mock models don't write code
    t = Tracer(tmp_path / "db.sqlite")
    tools = {r["data_json"] for r in t.query("SELECT data_json FROM events WHERE kind='tool'")}
    assert any('"run_tests"' in x for x in tools) and any('"search_docs"' in x for x in tools)
