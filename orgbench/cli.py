"""Command line interface: orgbench run | sweep | plan | report | org | task | models."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .catalog import Catalog
from .config import load_org
from .env import load_dotenv
from .paths import DEFAULT_RESULTS_DIR


def _kv(items: list[str] | None) -> dict[str, str]:
    out = {}
    for item in items or []:
        k, _, v = item.partition("=")
        if not v:
            raise SystemExit(f"expected ROLE=VALUE, got {item!r}")
        out[k.strip()] = v.strip()
    return out


def _org_from_args(args):
    org = load_org(args.org)
    return org.with_overrides(_kv(args.model), _kv(args.effort), args.set)


def _add_org_overrides(p):
    p.add_argument("--model", action="append", metavar="ROLE=MODEL",
                   help="swap a level's model (role name, 'top' or 'bottom'); repeatable")
    p.add_argument("--effort", action="append", metavar="ROLE=EFFORT", help="set a level's effort; repeatable")
    p.add_argument("--set", action="append", metavar="PATH=VALUE",
                   help="dotted override, e.g. levels.1.span=3 or communication.peers=true")


def _paths(args) -> tuple[Path, Path]:
    results = Path(args.results)
    return results, Path(args.db) if args.db else results / "orgbench.db"


def cmd_run(args) -> int:
    from .providers import ProviderRegistry
    from .runner import run_one
    from .tasks import make_task, parse_task_spec
    from .tracing import Tracer

    catalog = Catalog.load(args.models, offline=args.mock)
    org = _org_from_args(args)
    family, params = parse_task_spec(args.task)
    task = make_task(family, params, args.seed)
    results, db = _paths(args)
    print(f"org:  {org.name}  [{org.shape}]  {org.describe()}")
    print(f"task: {task.task_id}  (budget ${args.budget:.2f}{', MOCK' if args.mock else ''})")

    async def go():
        registry = ProviderRegistry(mock=args.mock)
        tracer = Tracer(db)
        try:
            return await run_one(org, task, catalog=catalog, registry=registry, tracer=tracer, results_dir=results,
                                 run_cap=args.budget, timeout_s=args.timeout)
        finally:
            await registry.aclose()
            tracer.close()

    s = asyncio.run(go())
    print(f"\nstatus {s['status']}  score {s['score']:.2f}  {'PASS' if s['passed'] else ''}")
    print(f"cost ${s['cost_usd']:.4f}  wall {s['wall_s']:.1f}s  calls {s['n_calls']}")
    if s.get("answer") is not None:
        print(f"answer: {s['answer']}")
    if task.family == "qa":
        print(f"expected: {task.gold()['expected']}")
    if s.get("error"):
        print(f"error: {s['error'][-800:]}")
    print(f"\n{'node':28} {'model':16} {'calls':>5} {'cost':>9}")
    for name, n in s["nodes"].items():
        print(f"{'  ' * n['level'] + name:28} {n['model'][:16]:16} {n.get('calls', 0):>5} ${n.get('cost', 0):>8.4f}")
    print(f"\ntranscript: {s['run_dir']}/transcript.jsonl")
    return 0 if s["status"] != "fatal" else 2


def cmd_plan(args) -> int:
    from .sweep import describe_plan, load_sweep, plan_sweep
    from .tracing import Tracer

    cfg, base = load_sweep(args.config)
    if args.budget:
        cfg.budget_usd = args.budget
    catalog = Catalog.load(args.models)
    runs = plan_sweep(cfg, base, catalog)
    _, db = _paths(args)
    tracer = Tracer(db) if db.exists() else None
    print(describe_plan(cfg, runs, catalog, tracer))
    return 0


def cmd_sweep(args) -> int:
    from .sweep import describe_plan, load_sweep, plan_sweep, run_sweep

    cfg, base = load_sweep(args.config)
    if args.budget is not None:
        cfg.budget_usd = args.budget
    if args.concurrency:
        cfg.concurrency = args.concurrency
    catalog = Catalog.load(args.models, offline=args.mock)
    runs = plan_sweep(cfg, base, catalog)
    print(describe_plan(cfg, runs, catalog))
    if not args.mock and not args.yes:
        reply = input(f"\nThis sweep can spend up to ${cfg.budget_usd:.2f} of real API credit. Continue? [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    results, db = _paths(args)
    sweep_id = asyncio.run(run_sweep(cfg, base, catalog=catalog, mock=args.mock, results_dir=results, db_path=db))
    if not args.no_report:
        from .analysis import write_report
        from .tracing import Tracer

        out = write_report(Tracer(db), sweep_id, results_dir=results)
        print(f"report: {out / 'report.md'}")
    return 0


def cmd_report(args) -> int:
    from .analysis import write_report
    from .tracing import Tracer

    results, db = _paths(args)
    out = write_report(Tracer(db), args.sweep, Path(args.out) if args.out else None, results_dir=results)
    print(f"report: {out / 'report.md'}")
    print((out / "report.md").read_text().split("## Score by task family")[0])
    return 0


def cmd_org(args) -> int:
    from .agents import build_tree

    org = _org_from_args(args)
    catalog = Catalog.load(args.models, offline=True)
    print(f"{org.name}: {org.describe()}")
    print(json.dumps(org.metrics(), indent=2))
    root = build_tree(org, catalog)
    if org.n_agents <= 64:
        for a in root.walk():
            print(f"{'  ' * a.level}{a.name}  [{a.model.alias}{'/' + a.spec.effort if a.spec.effort else ''}]")
    return 0


def cmd_task(args) -> int:
    from .tasks import make_task, parse_task_spec

    family, params = parse_task_spec(args.spec)
    task = make_task(family, params, args.seed)
    print(f"# {task.task_id}: {task.title}\n")
    print(task.brief)
    if args.gold:
        print("\n--- gold ---")
        print(json.dumps(task.gold(), indent=2, default=str))
    return 0


def cmd_models(args) -> int:
    catalog = Catalog.load(args.models)
    if args.action == "refresh":
        n = len(catalog.openrouter_models(refresh=True))
        print(f"cached {n} OpenRouter models")
        return 0
    live = {}
    if args.action == "check":
        live = catalog.openrouter_models(refresh=True)
    print(f"{'alias':16} {'provider':10} {'model':40} {'in $/M':>8} {'out $/M':>8}  notes")
    for alias, spec in catalog.models.items():
        note = ""
        if args.action == "check" and spec.provider == "openrouter":
            meta = live.get(spec.model)
            if meta is None:
                note = "NOT FOUND on OpenRouter"
            else:
                p = meta.get("pricing") or {}
                li, lo = float(p.get("prompt") or 0) * 1e6, float(p.get("completion") or 0) * 1e6
                if abs(li - spec.price.input) > 1e-6 or abs(lo - spec.price.output) > 1e-6:
                    note = f"live price {li:.4g}/{lo:.4g}"
                if "tools" not in (meta.get("supported_parameters") or []):
                    note += " NO TOOL SUPPORT"
        print(f"{alias:16} {spec.provider:10} {spec.model[:40]:40} {spec.price.input:>8.3f} {spec.price.output:>8.3f}  {note}")
    return 0


def cmd_ui(args) -> int:
    from .ui.server import serve

    results, _ = _paths(args)
    httpd = serve(args.host, args.port, Path(args.orgs_dir) if args.orgs_dir else None, results, args.models)
    host, port = httpd.server_address[:2]
    print(f"orgbench UI on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="orgbench", description=__doc__)
    ap.add_argument("--models", default=None, help="model catalog YAML (default: configs/models.yaml)")
    ap.add_argument("--results", default=str(DEFAULT_RESULTS_DIR), help="results directory (default: results)")
    ap.add_argument("--db", default=None, help="SQLite path (default: <results>/orgbench.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run one org on one task")
    p.add_argument("--org", required=True, help="org config YAML")
    p.add_argument("--task", required=True, help="task spec, e.g. qa:kind=chain,hops=3 | code:n=6 | mixed")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--budget", type=float, default=0.50, help="hard cap for this run in USD (default 0.50)")
    p.add_argument("--timeout", type=float, default=1200)
    p.add_argument("--mock", action="store_true", help="offline scripted models, no API calls")
    _add_org_overrides(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("plan", help="show what a sweep would run and its budget split")
    p.add_argument("config")
    p.add_argument("--budget", type=float)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("sweep", help="run a sweep")
    p.add_argument("config")
    p.add_argument("--budget", type=float, help="override budget_usd")
    p.add_argument("--concurrency", type=int)
    p.add_argument("--mock", action="store_true")
    p.add_argument("--yes", "-y", action="store_true", help="skip the spend confirmation")
    p.add_argument("--no-report", action="store_true")
    p.set_defaults(fn=cmd_sweep)

    p = sub.add_parser("report", help="build the report for a sweep (default: latest)")
    p.add_argument("sweep", nargs="?", default="latest", help="sweep id or name")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("org", help="show an org's structure and flatness metrics")
    p.add_argument("org")
    _add_org_overrides(p)
    p.set_defaults(fn=cmd_org)

    p = sub.add_parser("task", help="print a task brief (and gold answer)")
    p.add_argument("spec")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--gold", action="store_true")
    p.set_defaults(fn=cmd_task)

    p = sub.add_parser("ui", help="open the interactive org designer in your browser")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--orgs-dir", default=None, help="where org YAML files are read and saved (default configs/orgs)")
    p.set_defaults(fn=cmd_ui)

    p = sub.add_parser("models", help="list / check / refresh the model catalog")
    p.add_argument("action", nargs="?", default="list", choices=["list", "check", "refresh"])
    p.set_defaults(fn=cmd_models)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
