"""Run one org on one task: build the tree, run it under budget/timeout, grade, record."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .agents import Agent, build_tree
from .budget import RunBudget, RunBudgetExceeded
from .catalog import Catalog
from .config import OrgConfig
from .prompts import checkin_message, checkin_reason, root_message
from .providers import FatalProviderError, ProviderRegistry, StepResult, preflight
from .rules import WikiInstance, contacts, wiki_instances, wikis_for
from .runlog import close_run_log, describe_call, describe_event, explain_end, open_run_log
from .tasks import TaskInstance
from .tools import Toolbox
from .tracing import Tracer, Transcript, new_id
from .wiki import WikiStore

# Usage summed over a run's calls (the calls table is the one record of what each call used and cost).
USAGE = "COUNT(*) AS calls, " + ", ".join(
    f"COALESCE(SUM({c}), 0) AS {c}" for c in ("input_tokens", "output_tokens", "cache_read_tokens",
                                              "cache_write_tokens", "reasoning_tokens", "cost_usd", "latency_s",
                                              "rate_wait_s"))


@dataclass
class RunContext:
    run_id: str
    org: OrgConfig
    task: TaskInstance
    workspace: Path
    registry: ProviderRegistry
    budget: RunBudget
    tracer: Tracer
    transcript: Transcript
    tools: Toolbox = None  # type: ignore[assignment]
    listener: Callable[[dict], None] | None = None  # live UI hook
    log: logging.Logger | None = None  # runs/<run_id>/run.log
    wiki_dir: Path | None = None  # git repository for the wikis (run_one: runs/<run_id>/wiki)
    agents: dict[str, Agent] = field(default_factory=dict)
    wikis: list[WikiInstance] = field(default_factory=list)
    wiki: WikiStore = None  # type: ignore[assignment]
    inboxes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    messages_sent: Counter = field(default_factory=Counter)
    active: set[str] = field(default_factory=set)
    assign_counts: Counter = field(default_factory=Counter)
    delegate_rounds: Counter = field(default_factory=Counter)
    order_tasks: set = field(default_factory=set)  # every order running in the background
    _contacts: dict = field(default_factory=dict)
    _orders: int = 0

    def next_order(self) -> int:
        self._orders += 1
        return self._orders

    def bind(self, root: Agent) -> None:
        self.agents = {a.name: a for a in root.walk()}
        self.wikis = wiki_instances(self.org)
        self.wiki = WikiStore(self.wikis, self.wiki_dir or self.workspace.with_name(self.workspace.name + "-wiki"))
        for a in root.walk():
            self._contacts[a.name] = {b.name: k for b, k in contacts(self.org, a.node)}
        for inst in self.wikis:
            if inst.spec.publish_brief:
                self.wiki.write(inst.id, "Client brief", self.task.brief, author="client", role="client",
                                level=None, model=None)

    def wiki_access(self, agent: Agent) -> list[tuple[WikiInstance, bool]]:
        return wikis_for(self.wikis, agent.name)

    def contacts_of(self, agent: Agent) -> dict[str, str]:
        return self._contacts.get(agent.name, {})

    def event(self, agent: Agent, kind: str, data: dict | None = None) -> None:
        self.tracer.log_event(self.run_id, agent.name, agent.level, kind, data)
        if self.log:
            self.log.log(*describe_event(agent.name, kind, data or {}))
        if self.listener:
            self.listener({"type": "event", "node": agent.name, "kind": kind, "data": data or {}, "t": time.time()})

    def live(self, agent: Agent, state: str, data: dict | None = None) -> None:
        if self.listener:
            self.listener({"type": "state", "node": agent.name, "state": state, "data": data or {}, "t": time.time()})

    def log_call(self, agent: Agent, r: StepResult, cost: float, turn: int, max_tokens: int,
                 purpose: str = "work") -> None:
        u = r.usage
        self.tracer.log_call({
            "run_id": self.run_id, "node": agent.name, "level": agent.level, "role": agent.spec.role,
            "is_manager": int(bool(agent.children)), "model_alias": agent.model.alias, "model_id": agent.model.model,
            "served_model": r.served_model, "provider": agent.model.endpoint, "turn": turn, "purpose": purpose,
            "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens, "cache_write_tokens": u.cache_write_tokens,
            "reasoning_tokens": u.reasoning_tokens, "cost_usd": cost, "latency_s": r.latency_s,
            "rate_wait_s": r.rate_wait_s, "retries": r.retries, "max_tokens": max_tokens,
            "stop_reason": r.stop_reason, "n_tool_calls": len(r.tool_calls), "fallback": int(r.fallback),
        })
        if self.log:
            self.log.log(*describe_call(agent.name, agent.model.alias, turn, r, cost, max_tokens))
        if self.listener:
            group = "top" if agent.parent is None else "officer" if agent.children else "soldier"
            self.listener({"type": "call", "node": agent.name, "cost": cost, "purpose": purpose, "group": group,
                           "total_cost": self.budget.spent, "t": time.time(),
                           # tokens read (incl. cached) and generated (incl. reasoning) by this call
                           "tokens_in": u.input_tokens + u.cache_read_tokens + u.cache_write_tokens,
                           "tokens_out": u.output_tokens})


@dataclass
class Outcome:
    """How the mission went."""

    status: str = "completed"
    error: str | None = None
    fatal: FatalProviderError | None = None
    submission: dict | None = None  # the top position's last report
    checkins: int = 0


async def run_one(
    org: OrgConfig,
    task: TaskInstance,
    *,
    catalog: Catalog,
    registry: ProviderRegistry,
    tracer: Tracer,
    results_dir: Path,
    run_cap: float | None = 1.0,
    timeout_s: float = 1200,
    sweep_id: str | None = None,
    rep: int = 0,
    listener: Callable[[dict], None] | None = None,
    on_start: Callable[[RunContext], None] | None = None,
    echo: int | None = None,
    preflight: bool = True,
) -> dict:
    run_id = new_id("run")
    run_dir = results_dir / "runs" / run_id
    workspace = run_dir / "workspace"
    task.setup_workspace(workspace)
    root = build_tree(org, catalog, mock=registry.mock)
    log = open_run_log(run_id, run_dir / "run.log", echo)
    ctx = RunContext(run_id=run_id, org=org, task=task, workspace=workspace, registry=registry,
                     budget=RunBudget(run_cap), tracer=tracer, transcript=Transcript(run_dir / "transcript.jsonl"),
                     listener=listener, wiki_dir=run_dir / "wiki", log=log)
    ctx.tools = Toolbox(ctx)
    ctx.bind(root)
    if on_start:
        on_start(ctx)

    started, t0 = time.time(), time.monotonic()
    log.info("run %s: org %s (%s), task %s, budget %s, time limit %.0fs%s", run_id, org.name, org.shape,
             task.task_id, "none" if run_cap is None else f"${run_cap:.2f}", timeout_s, " [MOCK]" if registry.mock else "")
    for a in root.walk():
        log.info("  %s%s (%s) on %s, effort %s, max %d turns", "  " * a.level, a.name, a.spec.role, a.model.alias,
                 a.spec.effort or "default", a.max_turns(org))
    log.info("check-ins: up to %d%s", org.checkins.max, ", also when public tests fail" if org.checkins.verify_tests else "")
    problem = await _preflight(ctx, root) if preflight and not registry.mock else None
    if problem is not None:  # a model can't be used: stop before issuing the mission
        out = Outcome(status="fatal", error=problem.args[0], fatal=problem)
    else:
        out = await _mission(ctx, root, timeout_s)
    leftover = [t for t in ctx.order_tasks if not t.done()]  # e.g. after a timeout or an endpoint refusal
    for t in leftover:
        t.cancel()
    if leftover:
        await asyncio.gather(*leftover, return_exceptions=True)
    return await _record(ctx, root, out, started=started, wall=time.monotonic() - t0, timeout_s=timeout_s,
                         sweep_id=sweep_id, rep=rep)


async def _mission(ctx: RunContext, root: Agent, timeout_s: float) -> Outcome:
    """Give the mission to the top position and see it through: when it stops before the mission is done
    (or submits while the public tests still fail), the client checks in, up to checkins.max times."""
    task, rules, out = ctx.task, ctx.org.checkins, Outcome()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    ctx.event(root, "mission", {"order_id": 0, "to": root.name, "to_role": root.spec.role, "title": task.title,
                                "text": task.brief, "family": task.family})
    message = root_message(task)
    while True:
        try:
            async with asyncio.timeout_at(deadline):
                result = await root.handle(message, ctx, order_id=0)
        except Exception as e:  # time limit, budget, endpoint refusal, or a bug outside the agents
            status, error, fatal = _classify(e, timeout_s)
            if fatal is None and out.submission is not None:  # a check-in was cut short: keep the earlier report
                ctx.event(root, "checkin_skipped",
                          {"reason": f"check-in {out.checkins} was cut short: {error.splitlines()[0][:300]}"})
                if status == "error":
                    ctx.log.error("%s", error)
            else:
                out.status, out.error, out.fatal = status, error, fatal
            break
        out.submission = result
        if result["status"] == "refused" or out.checkins >= rules.max:
            break
        tests = None
        if rules.verify_tests and task.has_public_tests:
            passed, tests = await asyncio.to_thread(_public_check, task, ctx.workspace)
            if result["status"] == "submitted" and passed:
                break
        elif result["status"] == "submitted":
            break
        blocked = _checkin_blocked(ctx.budget, rules.min_budget_frac, deadline - loop.time(), timeout_s)
        if blocked:
            ctx.event(root, "checkin_skipped", {"reason": blocked})
            break
        out.checkins += 1
        reason = checkin_reason(result)
        ctx.event(root, "checkin", {"round": out.checkins, "max": rules.max, "status": result["status"],
                                    "reason": reason, "report": result["report"][:4000],
                                    "tests": tests[:3000] if tests else None})
        message = checkin_message(out.checkins, rules.max, reason, tests)

    s = out.submission
    if s is not None:
        ctx.event(root, "report", {"to": "client", "order_id": 0, "status": s["status"],
                                   "status_line": s.get("status_line"), "text": s["report"], "answer": s.get("answer"),
                                   "chars": len(s["report"]), "preview": s["report"][:240], "checkins": out.checkins})
        if out.status == "completed" and s["status"] != "submitted":
            out.status = s["status"]
            if out.status == "error":
                out.error = s["report"]
    return out


async def _record(ctx: RunContext, root: Agent, out: Outcome, *, started: float, wall: float, timeout_s: float,
                  sweep_id: str | None, rep: int) -> dict:
    """Grade the run and save it: the runs table, result.json, the end of run.log, and the listener."""
    run_id, org, task, tracer, log, run_cap = ctx.run_id, ctx.org, ctx.task, ctx.tracer, ctx.log, ctx.budget.cap
    run_dir = ctx.workspace.parent
    sub = out.submission
    crashes = len(tracer.query("SELECT 1 FROM events WHERE run_id=? AND kind='crash'", (run_id,)))
    end_reason = explain_end(out.status, out.error, root.name, sub["status"] if sub else None, out.checkins,
                             run_cap, ctx.budget.spent, timeout_s)
    answer = sub.get("answer") if sub else None
    grade = await asyncio.to_thread(task.grade, answer, ctx.workspace)
    undelivered = {k: len(v) for k, v in ctx.inboxes.items() if v}
    if undelivered:
        ctx.event(root, "undelivered", {"by_node": undelivered, "count": sum(undelivered.values())})
    ctx.wiki.close()  # every edit is already a commit in run_dir/wiki
    ctx.transcript.write(node="-", kind="result", status=out.status, answer=answer, score=grade.score, error=out.error)
    ctx.transcript.close()

    tot = tracer.query(f"SELECT {USAGE} FROM calls WHERE run_id=?", (run_id,))[0]
    metrics = org.metrics()
    row = {
        "run_id": run_id, "sweep_id": sweep_id, "org_name": org.name, "org_json": org.model_dump_json(),
        "shape": metrics["shape"], "depth": metrics["depth"], "n_agents": metrics["n_agents"],
        "n_workers": metrics["n_workers"], "communication": org.communication_summary(),
        "task_id": task.task_id, "family": task.family, "task_params": json.dumps(task.params), "seed": task.seed,
        "rep": rep, "status": out.status, "score": grade.score, "passed": int(grade.passed), "answer": answer,
        "grade_json": json.dumps(grade.details, default=str), "cost_usd": ctx.budget.spent, "budget_cap": run_cap,
        "n_calls": tot["calls"], "input_tokens": tot["input_tokens"], "output_tokens": tot["output_tokens"],
        "cache_read_tokens": tot["cache_read_tokens"], "cache_write_tokens": tot["cache_write_tokens"],
        "reasoning_tokens": tot["reasoning_tokens"], "wall_s": wall, "llm_s": tot["latency_s"],
        "rate_wait_s": tot["rate_wait_s"], "error": out.error, "mock": int(ctx.registry.mock),
        "end_reason": end_reason, "checkins": out.checkins, "crashes": crashes,
        "started_at": started, "finished_at": time.time(),
    }
    tracer.save_run(row)
    usage = {r["node"]: r for r in tracer.query(f"SELECT node, {USAGE} FROM calls WHERE run_id=? GROUP BY node",
                                                (run_id,))}
    nodes = {a.name: {"level": a.level, "role": a.spec.role, "model": a.model.alias,
                      "parent": a.parent.name if a.parent else None,
                      "calls": usage.get(a.name, {}).get("calls", 0),
                      "cost": round(usage.get(a.name, {}).get("cost_usd", 0.0), 6)}
             for a in root.walk()}
    summary = {**{k: row[k] for k in ("run_id", "org_name", "task_id", "status", "score", "passed", "answer",
                                     "cost_usd", "wall_s", "n_calls", "error", "end_reason", "checkins",
                                     "crashes")}, "mock": ctx.registry.mock,
               "grade": grade.details, "nodes": nodes, "run_dir": str(run_dir)}
    (run_dir / "result.json").write_text(json.dumps(summary, indent=2, default=str))
    level = logging.INFO if out.status == "completed" else logging.WARNING
    log.log(level, "run ended: %s. %s", out.status, end_reason)
    log.log(level, "score %s, cost $%.4f, %.1fs, %d calls, %d check-ins, %d crashes", grade.score, ctx.budget.spent,
            wall, tot["calls"], out.checkins, crashes)
    if out.error and out.status in ("error", "fatal"):
        log.debug("error detail:\n%s", out.error)
    close_run_log(log)
    if ctx.listener:
        ctx.listener({"type": "finished", "summary": dict(summary), "t": time.time()})
    if out.fatal is not None:
        summary["fatal"] = out.fatal
    return summary


def _leaves(e: BaseException):
    if isinstance(e, BaseExceptionGroup):
        for x in e.exceptions:
            yield from _leaves(x)
    else:
        yield e


def _classify(e: BaseException, timeout_s: float) -> tuple[str, str, FatalProviderError | None]:
    """(status, error, fatal exception) for an exception that reached the top of the run."""
    leaves = list(_leaves(e))
    for x in leaves:
        if isinstance(x, FatalProviderError):
            return "fatal", str(x), x
    for x in leaves:
        if isinstance(x, RunBudgetExceeded):
            return "budget_exhausted", str(x), None
    if any(isinstance(x, TimeoutError) for x in leaves):
        return "timeout", f"exceeded {timeout_s:.0f}s", None
    return "error", "".join(traceback.format_exception(leaves[0]))[-3000:], None


async def _preflight(ctx: RunContext, root: Agent) -> FatalProviderError | None:
    """Check every model once before the mission; a FatalProviderError listing each unusable one, or None."""
    specs = [a.model for a in root.walk()]
    results = await preflight(ctx.registry, specs)
    users: dict[str, list[str]] = {}
    for a in root.walk():
        users.setdefault(a.model.alias, []).append(a.name)
    bad = []
    for r in results:
        who = ", ".join(users.get(r["alias"], []))
        if r["skipped"]:
            ctx.log.debug("preflight: %s skipped (mock, or checked recently)", r["alias"])
        elif r["ok"]:
            ctx.log.info("preflight: %s (%s on %s) ok", r["alias"], r["model"], r["endpoint"])
        elif r["fatal"]:
            ctx.log.error("preflight: %s (%s on %s) can't be used: %s", r["alias"], r["model"], r["endpoint"], r["error"])
            bad.append(f"- {r['alias']} ({r['model']} on {r['endpoint']}), used by {who}: {r['error']}")
        else:
            ctx.log.warning("preflight: %s (%s on %s) failed, continuing anyway: %s", r["alias"], r["model"],
                            r["endpoint"], r["error"])
    if not bad:
        return None
    hint = ""
    if any("data policy" in b or "ZDR" in b or "guardrail" in b for b in bad):
        hint = ("\nYour OpenRouter privacy settings exclude every provider of these models. Allow them at "
                "https://openrouter.ai/settings/privacy, or swap them for models with matching providers "
                "(see the note in configs/models.yaml).")
    return FatalProviderError(f"Preflight: {len(bad)} of {len(results)} models can't be used, so nothing was run.\n"
                              + "\n".join(bad) + hint)


def _public_check(task: TaskInstance, workspace: Path) -> tuple[bool, str]:
    """Run the task's public tests (the same ones agents can run): (all passed, output)."""
    out = task.public_tests(workspace)
    return not re.search(r"\b(FAIL|ERROR)\b", out), out


def _checkin_blocked(budget: RunBudget, frac: float, time_left: float, timeout_s: float) -> str | None:
    if budget.cap is not None and budget.cap - budget.spent < frac * budget.cap:
        return f"only ${budget.cap - budget.spent:.4f} of the ${budget.cap:.2f} budget is left"
    if time_left < frac * timeout_s:
        return f"only {time_left:.0f}s of the {timeout_s:.0f}s time limit is left"
    return None
