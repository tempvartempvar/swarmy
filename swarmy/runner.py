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
from .rules import WikiInstance, contacts, wiki_instances, wikis_for
from .prompts import checkin_message, checkin_reason, root_message
from .runlog import close_run_log, describe_call, describe_event, explain_end, open_run_log
from .providers import FatalProviderError, ProviderRegistry, StepResult, preflight
from .tasks import TaskInstance
from .tools import Toolbox
from .tracing import Tracer, Transcript, new_id
from .wiki import WikiStore


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
    totals: Counter = field(default_factory=Counter)
    per_node: dict = field(default_factory=lambda: defaultdict(Counter))
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
            "served_model": r.served_model, "provider": agent.model.provider, "turn": turn, "purpose": purpose,
            "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens, "cache_write_tokens": u.cache_write_tokens,
            "reasoning_tokens": u.reasoning_tokens, "cost_usd": cost, "latency_s": r.latency_s,
            "rate_wait_s": r.rate_wait_s, "retries": r.retries, "max_tokens": max_tokens,
            "stop_reason": r.stop_reason, "n_tool_calls": len(r.tool_calls), "fallback": int(r.fallback),
        })
        for key, val in [("calls", 1), ("input_tokens", u.input_tokens), ("output_tokens", u.output_tokens),
                         ("cache_read_tokens", u.cache_read_tokens), ("cache_write_tokens", u.cache_write_tokens),
                         ("reasoning_tokens", u.reasoning_tokens)]:
            self.totals[key] += val
            self.per_node[agent.name][key] += val
        self.totals["llm_s"] += r.latency_s
        self.totals["rate_wait_s"] += r.rate_wait_s
        self.per_node[agent.name]["cost"] += cost
        if self.log:
            self.log.log(*describe_call(agent.name, agent.model.alias, turn, r, cost, max_tokens))
        if self.listener:
            self.listener({"type": "call", "node": agent.name, "cost": cost, "purpose": purpose,
                           "total_cost": self.budget.spent, "t": time.time()})


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
    transcript = Transcript(run_dir / "transcript.jsonl")
    log = open_run_log(run_id, run_dir / "run.log", echo)
    ctx = RunContext(run_id=run_id, org=org, task=task, workspace=workspace, registry=registry,
                     budget=RunBudget(run_cap), tracer=tracer, transcript=transcript, listener=listener,
                     wiki_dir=run_dir / "wiki", log=log)
    ctx.tools = Toolbox(ctx)
    ctx.bind(root)
    if on_start:
        on_start(ctx)

    started, t0 = time.time(), time.monotonic()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    rules = org.checkins
    status, error, submission, fatal, checkins = "completed", None, None, None, 0
    log.info("run %s: org %s (%s), task %s, budget %s, time limit %.0fs%s", run_id, org.name, org.shape,
             task.task_id, "none" if run_cap is None else f"${run_cap:.2f}", timeout_s, " [MOCK]" if registry.mock else "")
    for a in root.walk():
        log.info("  %s%s (%s) on %s, effort %s, max %d turns", "  " * a.level, a.name, a.spec.role, a.model.alias,
                 a.spec.effort or "default", a.max_turns(org))
    log.info("check-ins: up to %d%s", rules.max, ", also when public tests fail" if rules.verify_tests else "")
    problem = await _preflight(ctx, root) if preflight and not registry.mock else None
    if problem is not None:  # a model can't be used: stop before issuing the mission
        status, error, fatal = "fatal", problem.args[0], problem
    else:
        ctx.event(root, "mission", {"order_id": 0, "to": root.name, "to_role": root.spec.role, "title": task.title,
                                    "text": task.brief, "family": task.family})
    message = root_message(task)
    while problem is None:
        try:
            async with asyncio.timeout_at(deadline):
                result = await root.handle(message, ctx, order_id=0)
        except Exception as e:  # time limit, budget, endpoint refusal, or a bug outside the agents
            st, err, fat = _classify(e, timeout_s)
            if fat is None and submission is not None:  # a check-in round was cut short: keep the earlier report
                ctx.event(root, "checkin_skipped", {"reason": f"check-in {checkins} was cut short: {err.splitlines()[0][:300]}"})
                if st == "error":
                    log.error("%s", err)
                break
            status, error, fatal = st, err, fat
            break
        submission = result
        if result["status"] == "refused" or checkins >= rules.max:
            break
        tests = None
        if rules.verify_tests and task.has_public_tests:
            passed, tests = await asyncio.to_thread(_public_check, task, workspace)
            if result["status"] == "submitted" and passed:
                break
        elif result["status"] == "submitted":
            break
        blocked = _checkin_blocked(ctx.budget, rules.min_budget_frac, deadline - loop.time(), timeout_s)
        if blocked:
            ctx.event(root, "checkin_skipped", {"reason": blocked})
            break
        checkins += 1
        reason = checkin_reason(result)
        ctx.event(root, "checkin", {"round": checkins, "max": rules.max, "status": result["status"], "reason": reason,
                                    "report": result["report"][:4000], "tests": tests[:3000] if tests else None})
        message = checkin_message(checkins, rules.max, reason, tests)
        if org.memory == "fresh":  # the top position starts from a blank conversation: repeat the brief
            message = root_message(task) + "\n\n---\n\n" + message
    if submission is not None:
        ctx.event(root, "report", {"to": "client", "order_id": 0, "status": submission["status"],
                                   "text": submission["report"], "answer": submission.get("answer"),
                                   "chars": len(submission["report"]), "preview": submission["report"][:240],
                                   "checkins": checkins})
        if status == "completed" and submission["status"] != "submitted":
            status = submission["status"]
            if status == "error":
                error = submission["report"]
    wall = time.monotonic() - t0
    crashes = len(ctx.tracer.query("SELECT 1 FROM events WHERE run_id=? AND kind='crash'", (run_id,)))
    end_reason = explain_end(status, error, root.name, submission["status"] if submission else None, checkins,
                             run_cap, ctx.budget.spent, timeout_s)

    answer = submission.get("answer") if submission else None
    grade = await asyncio.to_thread(task.grade, answer, workspace)
    undelivered = {k: len(v) for k, v in ctx.inboxes.items() if v}
    if undelivered:
        ctx.event(root, "undelivered", {"by_node": undelivered, "count": sum(undelivered.values())})
    ctx.wiki.close()  # every edit is already a commit in run_dir/wiki
    transcript.write(node="-", kind="result", status=status, answer=answer, score=grade.score, error=error)
    transcript.close()

    tot = ctx.totals
    metrics = org.metrics()
    row = {
        "run_id": run_id, "sweep_id": sweep_id, "org_name": org.name, "org_json": org.model_dump_json(),
        "shape": metrics["shape"], "depth": metrics["depth"], "n_agents": metrics["n_agents"],
        "n_workers": metrics["n_workers"], "communication": org.communication_summary(),
        "task_id": task.task_id, "family": task.family, "task_params": json.dumps(task.params), "seed": task.seed,
        "rep": rep, "status": status, "score": grade.score, "passed": int(grade.passed), "answer": answer,
        "grade_json": json.dumps(grade.details, default=str), "cost_usd": ctx.budget.spent, "budget_cap": run_cap,
        "n_calls": tot["calls"], "input_tokens": tot["input_tokens"], "output_tokens": tot["output_tokens"],
        "cache_read_tokens": tot["cache_read_tokens"], "cache_write_tokens": tot["cache_write_tokens"],
        "reasoning_tokens": tot["reasoning_tokens"], "wall_s": wall, "llm_s": tot["llm_s"],
        "rate_wait_s": tot["rate_wait_s"], "error": error, "mock": int(registry.mock),
        "end_reason": end_reason, "checkins": checkins, "crashes": crashes,
        "started_at": started, "finished_at": time.time(),
    }
    tracer.save_run(row)
    nodes = {a.name: {"level": a.level, "role": a.spec.role, "model": a.model.alias,
                      "parent": a.parent.name if a.parent else None,
                      **{k: round(v, 6) if isinstance(v, float) else v for k, v in ctx.per_node[a.name].items()}}
             for a in root.walk()}
    summary = {**{k: row[k] for k in ("run_id", "org_name", "task_id", "status", "score", "passed", "answer",
                                     "cost_usd", "wall_s", "n_calls", "error", "end_reason", "checkins",
                                     "crashes")}, "mock": registry.mock,
               "grade": grade.details, "nodes": nodes, "run_dir": str(run_dir)}
    (run_dir / "result.json").write_text(json.dumps(summary, indent=2, default=str))
    level = logging.INFO if status == "completed" else logging.WARNING
    log.log(level, "run ended: %s. %s", status, end_reason)
    log.log(level, "score %s, cost $%.4f, %.1fs, %d calls, %d check-ins, %d crashes", grade.score, ctx.budget.spent,
            wall, tot["calls"], checkins, crashes)
    if error and status in ("error", "fatal"):
        log.debug("error detail:\n%s", error)
    close_run_log(log)
    if fatal is not None:
        summary["fatal"] = fatal
    if listener:
        listener({"type": "finished", "summary": {k: v for k, v in summary.items() if k != "fatal"},
                  "t": time.time()})
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
