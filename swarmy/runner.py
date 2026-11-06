"""Run one org on one task: build the tree, run it under budget/timeout, grade, record."""

from __future__ import annotations

import asyncio
import json
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
from .prompts import root_message
from .providers import FatalProviderError, ProviderRegistry, StepResult
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

    def bind(self, root: Agent) -> None:
        self.agents = {a.name: a for a in root.walk()}
        self.wikis = wiki_instances(self.org)
        self.wiki = WikiStore(self.wikis)
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
) -> dict:
    run_id = new_id("run")
    run_dir = results_dir / "runs" / run_id
    workspace = run_dir / "workspace"
    task.setup_workspace(workspace)
    root = build_tree(org, catalog, mock=registry.mock)
    transcript = Transcript(run_dir / "transcript.jsonl")
    ctx = RunContext(run_id=run_id, org=org, task=task, workspace=workspace, registry=registry,
                     budget=RunBudget(run_cap), tracer=tracer, transcript=transcript, listener=listener)
    ctx.tools = Toolbox(ctx)
    ctx.bind(root)
    if on_start:
        on_start(ctx)

    started, t0 = time.time(), time.monotonic()
    status, error, submission, fatal = "completed", None, None, None
    try:
        async with asyncio.timeout(timeout_s):
            submission = await root.handle(root_message(task), ctx)
        if submission["status"] != "submitted":
            status = submission["status"]
    except* RunBudgetExceeded as eg:
        status, error = "budget_exhausted", str(eg.exceptions[0])
    except* TimeoutError:
        status, error = "timeout", f"exceeded {timeout_s}s"
    except* FatalProviderError as eg:
        status, error, fatal = "fatal", str(eg.exceptions[0]), eg.exceptions[0]
    except* Exception as eg:
        exc = eg.exceptions[0]
        status = "error"
        error = "".join(traceback.format_exception(exc))[-3000:]
    wall = time.monotonic() - t0

    answer = submission.get("answer") if submission else None
    grade = await asyncio.to_thread(task.grade, answer, workspace)
    undelivered = {k: len(v) for k, v in ctx.inboxes.items() if v}
    if undelivered:
        ctx.event(root, "undelivered", {"by_node": undelivered, "count": sum(undelivered.values())})
    ctx.wiki.save(run_dir / "wikis")
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
        "started_at": started, "finished_at": time.time(),
    }
    tracer.save_run(row)
    nodes = {a.name: {"level": a.level, "role": a.spec.role, "model": a.model.alias,
                      "parent": a.parent.name if a.parent else None,
                      **{k: round(v, 6) if isinstance(v, float) else v for k, v in ctx.per_node[a.name].items()}}
             for a in root.walk()}
    summary = {**{k: row[k] for k in ("run_id", "org_name", "task_id", "status", "score", "passed", "answer",
                                     "cost_usd", "wall_s", "n_calls", "error")},
               "grade": grade.details, "nodes": nodes, "run_dir": str(run_dir)}
    (run_dir / "result.json").write_text(json.dumps(summary, indent=2, default=str))
    if fatal is not None:
        summary["fatal"] = fatal
    if listener:
        listener({"type": "finished", "summary": {k: v for k, v in summary.items() if k != "fatal"},
                  "t": time.time()})
    return summary

