"""Wiki revision history: authorship and rank of every edit, persistence, CLI."""

import asyncio
import json
from pathlib import Path

from dulwich.repo import Repo

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


def test_revisions_record_author_rank_and_mode(tmp_path):
    store = WikiStore(wiki_instances(_org()), tmp_path / "wiki")
    store.write("orders", "Plan", "step 1", author="colonel", role="colonel", level=0, model="haiku")
    store.write("orders", "plan", "step 2", author="private-1", role="private", level=1, model="haiku",
                append=True)
    store.write("orders", "Plan", "rewritten", author="private-2", role="private", level=1, model="haiku")
    page = store.find("orders", "PLAN")
    assert [r.mode for r in page.revisions] == ["create", "append", "replace"]
    assert page.content == "rewritten"
    assert page.current.author == "private-2" and page.current.role == "private"
    assert page.contributors() == [("colonel", "colonel", 1), ("private-1", "private", 1),
                                   ("private-2", "private", 1)]
    assert "last edited by private-2 (private, level 1)" in page.byline()
    data = store.to_json()
    md = render_markdown(data["instances"][0], data["instances"][0]["pages"][0])
    assert "| 2 |" in md and "private-1 | private | 1 | haiku | append" in md
    revs = data["instances"][0]["pages"][0]["revisions"]  # every revision's content and diff, from git
    assert revs[1]["content"] == "step 1\n\nstep 2"
    assert revs[2]["diff"] == ["@@ -1,3 +1 @@", "-step 1", "-", "-step 2", "+rewritten"]


def test_every_edit_is_a_git_commit_and_reopens(tmp_path):
    store = WikiStore(wiki_instances(_org()), tmp_path / "wiki")
    store.write("orders", "Plan", "# Plan\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n<script>x()</script>",
                author="colonel", role="colonel", level=0, model="haiku", summary="First plan")
    store.write("orders", "Plan", "more", author="private-1", role="private", level=1, model="haiku", append=True)
    store.close()
    repo = Repo(str(tmp_path / "wiki"))
    log = [e.commit for e in repo.get_walker()]
    assert [c.author.decode() for c in log[:2]] == ["private-1 (private) <private-1@swarmy.local>",
                                                     "colonel (colonel) <colonel@swarmy.local>"]
    assert log[1].message.decode().startswith("First plan\n\nSwarmy-Wiki: orders\nSwarmy-Page: Plan\n")
    assert len(log) == 3 and not log[0].gpgsig  # plus the initial commit; never signed with the user's key
    data = load(tmp_path)  # rebuilt from git alone
    page = data["instances"][0]["pages"][0]
    assert data["repo"] == str((tmp_path / "wiki").resolve()) and page["path"] == "orders/plan.md"
    r1, r2 = page["revisions"]
    assert (r1["author"], r1["role"], r1["level"], r1["summary"]) == ("colonel", "colonel", 0, "First plan")
    assert (r2["author"], r2["mode"], r2["summary"]) == ("private-1", "append", "")
    assert "<table>" in r1["html"] and "<script>" not in r1["html"] and "&lt;script&gt;" in r1["html"]
    assert r2["diff"][-2:] == ["+", "+more"]


def test_a_run_without_pages_still_lists_its_wikis(tmp_path):
    WikiStore(wiki_instances(_org()), tmp_path / "wiki").close()
    data = load(tmp_path)  # the repository records the run's wikis, so the explorer can name them
    assert [i["name"] for i in data["instances"]] == ["orders"] and not data["instances"][0]["pages"]
    assert load(tmp_path / "missing") is None
    (tmp_path / "stopped" / "wiki").mkdir(parents=True)
    Repo.init(str(tmp_path / "stopped" / "wiki"))  # a run stopped before its first wiki commit
    assert load(tmp_path / "stopped") is None


def test_relative_repo_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the UI and CLI pass a relative results directory
    store = WikiStore(wiki_instances(_org()), Path("results/runs/r1/wiki"))
    store.write("orders", "Plan", "step 1", author="colonel", role="colonel", level=0, model="haiku")
    store.close()
    data = load(Path("results/runs/r1"))
    assert data["instances"][0]["pages"][0]["revisions"][0]["content"] == "step 1"


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
    assert (run_dir / "wiki" / "orders" / "plan.md").read_text() == titles["Plan"]["revisions"][-1]["content"]
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

