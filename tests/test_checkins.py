"""Check-ins with the top position, contained crashes, the run log and why a run ended."""

import asyncio
import json

from swarmy.agents import Agent
from swarmy.catalog import Catalog
from swarmy.cli import main
from swarmy.config import OrgConfig
from swarmy.orders import load_orders
from swarmy.providers import FatalProviderError, ProviderRegistry
from swarmy.runner import run_one
from swarmy.tasks import make_task
from swarmy.tools import Toolbox
from swarmy.tracing import Tracer


def _org(root_turns=None, **extra):
    tree = {"role": "field-marshal", "model": "mock", "children": [
        {"role": "captain", "model": "mock", "children": [{"role": "private", "model": "mock"}] * 2}]}
    if root_turns:
        tree["max_turns"] = root_turns
    return OrgConfig.model_validate({"name": "c", "tree": tree,
                                     "knowledge": {"wikis": [{"name": "orders", "scope": "org"}]}, **extra})


def _run(org, tmp_path, task=("mission", {"text": "Write a short plan."}), budget=1.0):
    async def go():
        reg = ProviderRegistry(mock=True)
        try:
            return await run_one(org, make_task(task[0], task[1], 1), catalog=Catalog.load(offline=True), registry=reg,
                                 tracer=Tracer(tmp_path / "db.sqlite"), results_dir=tmp_path, run_cap=budget)
        finally:
            await reg.aclose()
    s = asyncio.run(go())
    t = Tracer(tmp_path / "db.sqlite")
    events = [(r["node"], r["kind"], json.loads(r["data_json"]))
              for r in t.query("SELECT node, kind, data_json FROM events WHERE run_id=? ORDER BY id", (s["run_id"],))]
    log = (tmp_path / "runs" / s["run_id"] / "run.log").read_text()
    return s, events, log, t


def test_top_position_is_checked_in_on_after_hitting_its_turn_limit(tmp_path):
    s, events, log, _ = _run(_org(root_turns=1), tmp_path)
    checkins = [d for _, k, d in events if k == "checkin"]
    assert [c["round"] for c in checkins] == [1, 2] and all(c["status"] == "turn_limit" for c in checkins)
    assert "used all of your turns" in checkins[0]["reason"]
    assert s["status"] == "turn_limit" and s["checkins"] == 2
    assert "used all of its turns without submitting after 2 check-ins" in s["end_reason"]
    assert "check-in 1/2 with field-marshal" in log and "run ended: turn_limit" in log
    # the top position got the check-in as a new user turn and kept its conversation
    assert sum(1 for _, k, d in events if k == "agent_done" and d["order_id"] == 0) == 3


def test_checkins_can_be_turned_off_and_respect_the_budget(tmp_path):
    s, events, _, _ = _run(_org(root_turns=1, checkins={"max": 0}), tmp_path / "a")
    assert s["checkins"] == 0 and not [k for _, k, _ in events if k.startswith("checkin")]
    s, events, log, _ = _run(_org(root_turns=1, checkins={"min_budget_frac": 1.0}), tmp_path / "b")
    skipped = [d for _, k, d in events if k == "checkin_skipped"]
    # mock runs cost nothing, so here it's the time-limit guard that stops the check-in
    assert s["checkins"] == 0 and skipped and "time limit is left" in skipped[0]["reason"]
    assert "no check-in: only" in log


def test_submitting_with_failing_public_tests_triggers_a_checkin(tmp_path):
    org = _org()
    s, events, log, t = _run(org, tmp_path / "a", task=("greenhouse", {}))
    checkins = [d for _, k, d in events if k == "checkin"]
    assert len(checkins) == 2 and checkins[0]["status"] == "submitted"
    assert "public tests still fail" in checkins[0]["reason"] and "sim.py:" in checkins[0]["tests"]
    assert s["status"] == "completed" and "public tests:" in log
    mission = load_orders(t, s["run_id"])["orders"][0]
    assert len(mission["checkins"]) == 2 and mission["checkins"][0]["tests"]
    s, events, _, _ = _run(_org(checkins={"verify_tests": False}), tmp_path / "b", task=("greenhouse", {}))
    assert s["checkins"] == 0


def test_a_crashing_tool_is_reported_to_the_agent_not_fatal(tmp_path, monkeypatch):
    def boom(self, agent, args):
        raise IsADirectoryError(21, "Is a directory", "notes/")
    monkeypatch.setattr(Toolbox, "_t_wiki_write", boom)
    s, events, log, _ = _run(_org(), tmp_path)
    crashes = [d for _, k, d in events if k == "crash"]
    assert crashes and crashes[0]["where"] == "tool wiki_write" and "IsADirectoryError" in crashes[0]["error"]
    assert s["status"] == "completed" and s["crashes"] == len(crashes)
    assert "Traceback (most recent call last)" in log and "crashed in tool wiki_write" in log


def test_a_crashing_agent_reports_an_error_upward_and_the_run_goes_on(tmp_path, monkeypatch):
    original = Agent._deliver_inbox

    def flaky(self, ctx):
        if self.name == "private-1.1":
            raise KeyError("unexpected response shape")
        return original(self, ctx)
    monkeypatch.setattr(Agent, "_deliver_inbox", flaky)
    s, events, _, _ = _run(_org(), tmp_path)
    done = {n: d["status"] for n, k, d in events if k == "agent_done"}
    assert done["private-1.1"] == "error" and done["private-1.2"] == "submitted"
    reports = [d for n, k, d in events if k == "report" and n == "private-1.1"]
    assert reports and "internal error KeyError" in reports[0]["text"]
    assert s["status"] == "completed" and s["crashes"] >= 1


def test_an_endpoint_refusal_still_stops_the_run(tmp_path, monkeypatch):
    original = Agent._deliver_inbox

    def refused(self, ctx):
        if self.name == "private-1.2":
            raise FatalProviderError("openrouter: insufficient credits: add credits to continue")
        return original(self, ctx)
    monkeypatch.setattr(Agent, "_deliver_inbox", refused)
    s, _, log, _ = _run(_org(), tmp_path)
    assert s["status"] == "fatal" and s["checkins"] == 0
    assert s["end_reason"].startswith("The model endpoint refused the run") and "insufficient credits" in s["end_reason"]
    assert "run ended: fatal" in log


def test_cli_log_and_run_summary(tmp_path, capsys):
    assert main(["--results", str(tmp_path), "run", "--org", "configs/orgs/regiment.yaml", "--task",
                 "qa:kind=chain,hops=2", "--mock"]) == 0
    out = capsys.readouterr().out
    assert "why it ended:" in out and "run.log" in out
    assert main(["--results", str(tmp_path), "log"]) == 0
    out = capsys.readouterr().out
    assert "run ended:" in out and " DEBUG " not in out
    assert main(["--results", str(tmp_path), "log", "--all"]) == 0
    assert " DEBUG " in capsys.readouterr().out


def test_checkin_guard_reports_low_budget():
    from swarmy.budget import RunBudget
    from swarmy.runner import _checkin_blocked
    b = RunBudget(1.0)
    b.charge(0.9)
    assert "budget is left" in _checkin_blocked(b, 0.15, 1000, 1200)
    assert _checkin_blocked(RunBudget(1.0), 0.15, 1000, 1200) is None
    assert "time limit" in _checkin_blocked(RunBudget(None), 0.15, 60, 1200)

