"""Sweeps: orgs x tasks x seeds x repeats under one dollar budget."""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from .budget import SweepBudget
from .catalog import Catalog
from .config import GenerateSpec, OrgConfig, load_org
from .paths import REPO_ROOT
from .providers import ProviderRegistry
from .runner import run_one
from .tasks import TaskInstance, make_task
from .tracing import Tracer, new_id


class OrgEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str | None = None
    generate: GenerateSpec | None = None
    name: str | None = None
    models: dict[str, str] = {}
    efforts: dict[str, str | None] = {}
    set: list[str] = []
    # Cartesian sweep over model/effort choices: {"worker": [a, b], "effort.ceo": [high, max]}
    matrix: dict[str, list] = {}
    run_budget_usd: float | None = None

    def build(self, base: Path) -> list[OrgConfig]:
        if self.path:
            p = Path(self.path)
            if not p.is_absolute():
                p = (base / p) if (base / p).exists() else (REPO_ROOT / p)
            orgs = [load_org(p)]
        elif self.generate:
            orgs = self.generate.build()
        else:
            raise ValueError("org entry needs `path` or `generate`")
        out = []
        for org in orgs:
            org = org.with_overrides(self.models, self.efforts, self.set,
                                     name=self.name if (self.name and len(orgs) == 1 and not self.matrix) else None)
            if not self.matrix:
                out.append(org)
                continue
            keys = list(self.matrix)
            for combo in itertools.product(*(self.matrix[k] for k in keys)):
                models = {k: v for k, v in zip(keys, combo) if not k.startswith("effort.")}
                efforts = {k.split(".", 1)[1]: v for k, v in zip(keys, combo) if k.startswith("effort.")}
                out.append(org.with_overrides(models, efforts))
        return out


class TaskEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: str
    params: dict = {}
    seeds: list[int] = [1]


class SweepConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    budget_usd: float = 5.0
    run_budget_usd: float | None = None
    concurrency: int = 2
    repeats: int = 1
    run_timeout_s: float = 1200
    orgs: list[OrgEntry]
    tasks: list[TaskEntry]


def load_sweep(path: str | Path) -> tuple[SweepConfig, Path]:
    path = Path(path)
    return SweepConfig.model_validate(yaml.safe_load(path.read_text())), path.parent


@dataclass
class PlannedRun:
    org: OrgConfig
    task: TaskInstance
    rep: int
    cap: float


def plan_sweep(cfg: SweepConfig, base: Path, catalog: Catalog) -> list[PlannedRun]:
    orgs: list[tuple[OrgConfig, float | None]] = []
    for entry in cfg.orgs:
        for org in entry.build(base):
            for lvl in org.levels:
                catalog.resolve(lvl.model)  # fail early on unknown models
            orgs.append((org, entry.run_budget_usd))
    names = [o.name for o, _ in orgs]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate org names in sweep: {sorted(dupes)} (set `name:` on the entries)")
    tasks = [make_task(t.family, t.params, s) for t in cfg.tasks for s in t.seeds]
    n_runs = len(orgs) * len(tasks) * cfg.repeats
    default_cap = cfg.run_budget_usd or (cfg.budget_usd / max(1, n_runs))
    # Interleave (rep -> task -> org) so that if the budget runs out, every org has data on the same tasks.
    return [PlannedRun(org, task, rep, cap or default_cap)
            for rep in range(cfg.repeats) for task in tasks for org, cap in orgs]


def describe_plan(cfg: SweepConfig, runs: list[PlannedRun], catalog: Catalog, tracer: Tracer | None = None) -> str:
    lines = [f"Sweep '{cfg.name}': {len(runs)} runs, budget ${cfg.budget_usd:.2f}, concurrency {cfg.concurrency}"]
    seen = {}
    for r in runs:
        seen.setdefault(r.org.name, (r.org, r.cap))
    lines.append(f"\n{'org':34} {'shape':16} {'agents':>6} {'cap/run':>8}  structure")
    for name, (org, cap) in seen.items():
        lines.append(f"{name[:34]:34} {org.shape:16} {org.n_agents:>6} {f'${cap:.3f}':>8}  {org.describe()}")
        for lvl in org.levels:
            spec = catalog.resolve(lvl.model)
            inp, out, _, cw = spec.price.rates(4000)
            one_call = 4000 * max(inp, cw) + lvl.output_cap() * out
            if one_call > cap * 0.5:
                lines.append(f"{'':34}   ! a single {lvl.role} call can cost up to ${one_call:.3f}; the run cap "
                             f"will clamp its max_tokens")
    tasks = sorted({r.task.task_id for r in runs})
    lines.append(f"\ntasks ({len(tasks)}): " + ", ".join(tasks))
    caps = sum(r.cap for r in runs)
    if caps > cfg.budget_usd + 1e-6:
        lines.append(f"\nnote: run caps sum to ${caps:.2f} > budget; once the budget is committed, remaining runs "
                     f"are skipped (unused cap is returned after each run).")
    if tracer is not None:
        est, known = 0.0, 0
        for r in runs:
            rows = tracer.query("SELECT AVG(cost_usd) c FROM runs WHERE org_name=? AND family=? AND mock=0 "
                                "AND status NOT LIKE 'skipped%'", (r.org.name, r.task.family))
            if rows and rows[0]["c"] is not None:
                est += rows[0]["c"]
                known += 1
        if known:
            lines.append(f"\nhistorical estimate for {known}/{len(runs)} runs with past data: ${est:.2f}")
    return "\n".join(lines)


async def run_sweep(cfg: SweepConfig, base: Path, *, catalog: Catalog, mock: bool, results_dir: Path,
                    db_path: Path, log=print) -> str:
    runs = plan_sweep(cfg, base, catalog)
    tracer = Tracer(db_path)
    sweep_id = new_id("sweep")
    tracer.start_sweep(sweep_id, cfg.name, cfg.model_dump(), cfg.budget_usd, mock)
    budget = SweepBudget(cfg.budget_usd)
    registry = ProviderRegistry(mock=mock)
    queue = list(enumerate(runs))
    stop = asyncio.Event()
    t_start = time.monotonic()
    log(f"sweep {sweep_id}: {len(runs)} runs, budget ${cfg.budget_usd:.2f}{' (mock)' if mock else ''}")

    def skip(pr: PlannedRun, status: str) -> None:
        m = pr.org.metrics()
        tracer.save_run({"run_id": new_id("run"), "sweep_id": sweep_id, "org_name": pr.org.name,
                         "org_json": pr.org.model_dump_json(), "shape": m["shape"], "depth": m["depth"],
                         "n_agents": m["n_agents"], "n_workers": m["n_workers"], "communication": pr.org.communication,
                         "task_id": pr.task.task_id, "family": pr.task.family, "task_params": json.dumps(pr.task.params),
                         "seed": pr.task.seed, "rep": pr.rep, "status": status, "cost_usd": 0.0,
                         "budget_cap": pr.cap, "mock": int(mock), "started_at": time.time()})

    async def worker() -> None:
        while queue:
            idx, pr = queue.pop(0)
            if stop.is_set():
                skip(pr, "skipped_fatal")
                continue
            if not await budget.reserve(pr.cap):
                skip(pr, "skipped_budget")
                log(f"[{idx + 1:>3}/{len(runs)}] skipped (budget): {pr.org.name} x {pr.task.task_id}")
                continue
            actual = 0.0
            try:
                s = await run_one(pr.org, pr.task, catalog=catalog, registry=registry, tracer=tracer,
                                  results_dir=results_dir, run_cap=pr.cap, timeout_s=cfg.run_timeout_s,
                                  sweep_id=sweep_id, rep=pr.rep)
                actual = s["cost_usd"]
                mark = "PASS" if s["passed"] else "    "
                log(f"[{idx + 1:>3}/{len(runs)}] {mark} score={s['score']:.2f} ${actual:.4f} {s['wall_s']:6.1f}s "
                    f"{s['status']:<16} {pr.org.name} x {pr.task.task_id}")
                if s.get("fatal") is not None:
                    log(f"FATAL: {s['fatal']} - stopping sweep")
                    stop.set()
            except Exception as e:  # a bug in the harness; keep the sweep going
                log(f"[{idx + 1:>3}/{len(runs)}] harness error: {e!r}")
            finally:
                await budget.settle(pr.cap, actual)
            log(f"      sweep spend ${budget.spent:.4f} / ${budget.total:.2f}")

    try:
        await asyncio.gather(*(worker() for _ in range(max(1, cfg.concurrency))))
    finally:
        await registry.aclose()
        tracer.finish_sweep(sweep_id, budget.spent)
    log(f"sweep {sweep_id} done in {time.monotonic() - t_start:.0f}s, spent ${budget.spent:.4f}")
    tracer.close()
    return sweep_id
