"""Wiki revision history: authorship and rank of every edit, persistence, CLI."""

import asyncio
import json

from swarmy.catalog import Catalog
from swarmy.cli import main
from swarmy.config import OrgConfig, load_org
from swarmy.paths import CONFIG_DIR
from swarmy.providers import ProviderRegistry
from swarmy.rules import wiki_instances
from swarmy.runner import run_one
from swarmy.tasks import make_task
from swarmy.tracing import Tracer
from swarmy.wiki import WikiStore, load, render_markdown


def _org():
    return OrgConfig.model_validate({"name": "w", "levels": [
        {"role": "colonel", "model": "haiku", "span": 2}, {"role": "private", "model": "haiku"}],
        "knowledge": {"wikis": [{"name": "orders", "scope": "org"}]}})


def test_revisions_record_author_rank_and_mode():
    store = WikiStore(wiki_instances(_org()))
    store.write("orders", "Plan", "step 1", author="colonel", role="colonel", level=0, model="haiku")
    store.write("orders", "plan", "step 2", author="private-1", role="private", level=1, model="haiku",
                append=True)
    store.write("orders", "Plan", "rewritten", author="private-2", role="private", level=1, model="haiku")
    page = store.find("orders", "PLAN")
    assert [r.mode for r in page.revisions] == ["create", "append", "replace"]
    assert page.revisions[1].content == "step 1\n\nstep 2"
    assert page.current.author == "private-2" and page.current.role == "private"
    assert page.contributors() == [("colonel", "colonel", 1), ("private-1", "private", 1),
                                   ("private-2", "private", 1)]
    assert "last edited by private-2 (private, level 1)" in page.byline()
    data = store.to_json()
    md = render_markdown(data["instances"][0], data["instances"][0]["pages"][0])
    assert "| 2 |" in md and "private-1 | private | 1 | haiku | append" in md


def _mock_run(org, tmp_path):
    async def go():
        reg = ProviderRegistry(mock=True)
        try:
            return await run_one(org, make_task("mixed", {}, 1), catalog=Catalog.load(offline=True), registry=reg,
                                 tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=1.0)
        finally:
            await reg.aclose()
    return asyncio.run(go())


def test_run_saves_wiki_history_and_agents_see_ranks(tmp_path):
    org = load_org(CONFIG_DIR / "orgs" / "general-staff.yaml")
    s = _mock_run(org, tmp_path)
    run_dir = tmp_path / "runs" / s["run_id"]
    data = load(run_dir)
    orders = next(i for i in data["instances"] if i["id"] == "orders")
    titles = {p["title"]: p for p in orders["pages"]}
    assert titles["Client brief"]["revisions"][0]["author"] == "client"
    plan = titles["Plan"]["revisions"]
    assert {r["role"] for r in plan} <= {"field-marshal", "colonel"} and all(r["level"] in (0, 1) for r in plan)
    assert (run_dir / "wikis" / "orders" / "Plan.md").read_text().count("| ") > 5
    # what an agent reads includes the byline and contributors with their ranks
    transcript = (run_dir / "transcript.jsonl").read_text().splitlines()
    reads = [json.loads(l) for l in transcript if '"tool": "wiki_read"' in l]
    assert any("Pages in" in r["content"] and "last edited by" in r["content"] for r in reads)
    # write events carry the editor's rank
    t = Tracer(tmp_path / "db.sqlite")
    ev = [json.loads(r["data_json"]) for r in t.query("SELECT data_json FROM events WHERE kind='wiki_write'")]
    assert ev and all("role" in e and "version" in e for e in ev)


def test_cli_wiki(tmp_path, capsys):
    _mock_run(load_org(CONFIG_DIR / "orgs" / "general-staff.yaml"), tmp_path)
    assert main(["--results", str(tmp_path), "wiki"]) == 0
    out = capsys.readouterr().out
    assert "orders  (whole organization)" in out and "[colonel]" in out
    assert main(["--results", str(tmp_path), "wiki", "--wiki", "orders", "--page", "Plan"]) == 0
    out = capsys.readouterr().out
    assert "## Revision history" in out and "| rank |" in out
