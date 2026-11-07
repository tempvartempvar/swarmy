"""Command line interface: swarmy run | sweep | plan | report | org | task | models."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
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
    return results, Path(args.db) if args.db else results / "swarmy.db"


def cmd_run(args) -> int:
    from .providers import ProviderRegistry
    from .runner import run_one
    from .tasks import make_task, parse_task_spec
    from .tracing import Tracer

    catalog = Catalog.load(args.models, offline=args.mock)
    org = _org_from_args(args)
    if args.mission or args.mission_file:
        text = args.mission or Path(args.mission_file).read_text()
        task = make_task("mission", {"text": text}, args.seed)
    elif args.task:
        family, params = parse_task_spec(args.task)
        task = make_task(family, params, args.seed)
    else:
        raise SystemExit("give --task SPEC or --mission TEXT")
    results, db = _paths(args)
    print(f"org:  {org.name}  [{org.shape}]  {org.describe()}")
    print(f"task: {task.task_id}  (budget ${args.budget:.2f}{', MOCK' if args.mock else ''})")

    async def go():
        registry = ProviderRegistry(mock=args.mock)
        tracer = Tracer(db)
        try:
            return await run_one(org, task, catalog=catalog, registry=registry, tracer=tracer, results_dir=results,
                                 run_cap=args.budget, timeout_s=args.timeout,
                                 echo={0: None, 1: logging.INFO}.get(args.verbose, logging.DEBUG),
                                 preflight=not args.no_preflight)
        finally:
            await registry.aclose()
            tracer.close()

    s = asyncio.run(go())
    score = "not graded" if s["score"] is None else f"{s['score']:.2f}"
    print(f"\nstatus {s['status']}  score {score}  {'PASS' if s['passed'] else ''}")
    print(f"why it ended: {s['end_reason']}")
    print(f"cost ${s['cost_usd']:.4f}  wall {s['wall_s']:.1f}s  calls {s['n_calls']}")
    if s.get("answer") is not None:
        print(f"answer: {s['answer']}")
    if task.family == "qa":
        print(f"expected: {task.gold()['expected']}")
    if s.get("error") and s["status"] in ("error", "fatal"):
        print(f"error: {s['error'][-800:]}")
    print(f"\n{'node':28} {'model':16} {'calls':>5} {'cost':>9}")
    for name, n in s["nodes"].items():
        print(f"{'  ' * n['level'] + name:28} {n['model'][:16]:16} {n.get('calls', 0):>5} ${n.get('cost', 0):>8.4f}")
    print(f"\nlog:        {s['run_dir']}/run.log   (swarmy log {s['run_id']})")
    print(f"transcript: {s['run_dir']}/transcript.jsonl")
    if task.family == "mission":
        print(f"workspace:  {s['run_dir']}/workspace   (orders: swarmy orders {s['run_id']})")
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
    if args.action == "endpoints":
        print(f"{'endpoint':14} {'type':10} {'key':6} {'source':10} base url")
        for name, ep in catalog.endpoints.items():
            key = "-" if ep.api_key_env is None else ("set" if ep.key_present else "MISSING")
            print(f"{name:14} {ep.type:10} {key:6} {ep.source:10} {ep.base_url or '(SDK default)'}"
                  + (f"   [{ep.api_key_env}]" if ep.api_key_env else ""))
        return 0
    if args.action == "test":
        if not args.name:
            raise SystemExit("usage: swarmy models test <alias or endpoint:model>")
        from .providers import probe

        spec = catalog.resolve(args.name)
        print(f"testing {args.name} ({spec.endpoint}:{spec.model}) with one short real call...")
        r = asyncio.run(probe(spec))
        if r["ok"]:
            print(f"OK in {r['latency_s']}s: {r['text']!r}  ({r['input_tokens']} in / {r['output_tokens']} out, "
                  f"${r['cost_usd']:.6f}, served by {r['served_model']})")
            return 0
        print(f"FAILED after {r['latency_s']}s: {r['error']}")
        return 1
    live_eps = [n for n, ep in catalog.endpoints.items() if ep.live_pricing]
    if args.action == "refresh":
        for name in live_eps:
            print(f"cached {len(catalog.live_models(name, refresh=True))} {name} models")
        return 0
    live = {n: catalog.live_models(n, refresh=True) for n in live_eps} if args.action == "check" else {}
    print(f"{'alias':16} {'endpoint':11} {'model':40} {'in $/M':>8} {'out $/M':>8}  notes")
    for alias, spec in catalog.models.items():
        note = "" if spec.price_known else "price unknown (budget guard can't limit it)"
        if spec.endpoint in live:
            meta = live[spec.endpoint].get(spec.model)
            if meta is None:
                note = f"NOT FOUND on {spec.endpoint}"
            else:
                p = meta.get("pricing") or {}
                li, lo = float(p.get("prompt") or 0) * 1e6, float(p.get("completion") or 0) * 1e6
                if abs(li - spec.rate.input) > 1e-6 or abs(lo - spec.rate.output) > 1e-6:
                    note = f"live price {li:.4g}/{lo:.4g}"
                if "tools" not in (meta.get("supported_parameters") or []):
                    note += " NO TOOL SUPPORT"
        if spec.endpoint_cfg and not spec.endpoint_cfg.key_present:
            note += f" ({spec.endpoint_cfg.api_key_env} not set)"
        print(f"{alias:16} {spec.endpoint:11} {spec.model[:40]:40} {spec.rate.input:>8.3f} {spec.rate.output:>8.3f}  {note}")
    return 0


def cmd_orders(args) -> int:
    from .orders import load_orders, render
    from .tracing import Tracer

    _, db = _paths(args)
    tracer = Tracer(db)
    run_id = args.run
    if run_id in (None, "latest"):
        rows = tracer.query("SELECT run_id FROM events WHERE kind='mission' ORDER BY id DESC LIMIT 1")
        if not rows:
            raise SystemExit("no runs with recorded orders yet")
        run_id = rows[0]["run_id"]
    data = load_orders(tracer, run_id)
    print(f"run {run_id}  {data['task_id'] or ''}  [{data['status']}]\n")
    print(render(data, full=args.full))
    return 0


def cmd_wiki(args) -> int:
    from .wiki import load, render_markdown

    results, _ = _paths(args)
    runs_dir = results / "runs"
    if args.run in (None, "latest"):
        found = sorted(runs_dir.glob("run_*/result.json"), key=lambda f: f.stat().st_mtime)
        if not found:
            raise SystemExit(f"no runs under {runs_dir}")
        run_dir = found[-1].parent
    else:
        run_dir = runs_dir / args.run
    data = load(run_dir)
    if data is None:
        from .tracing import Tracer
        from .wiki import activity

        _, db = _paths(args)
        act = activity(Tracer(db), run_dir.name)
        print(f"{run_dir.name} has no wiki pages. Wiki activity: {act['reads']} reads, {act['writes']} writes, "
              f"{len(act['failed'])} failed calls, {act['reminders']} submit reminders.")
        for f in act["failed"][:10]:
            print(f"  {f['node']}: {f['tool']} failed: {f['error']}")
        return 1
    print(f"run {run_dir.name}" + (f"  (git repository: {data['repo']})" if data.get("storage") == "git" else "") + "\n")
    for inst in data["instances"]:
        if args.wiki and inst["id"] != args.wiki:
            continue
        if not inst["pages"]:
            continue
        if args.page:
            for page in inst["pages"]:
                if page["title"].lower() == args.page.lower():
                    print(render_markdown(inst, page))
            continue
        print(f"{inst['id']}  ({inst['label']})")
        for page in inst["pages"]:
            cur = page["revisions"][-1]
            people = sorted({f"{r['author']} [{r['role']}]" for r in page["revisions"]})
            print(f"  - {page['title']}: v{cur['version']}, last by {cur['author']} [{cur['role']}], "
                  f"{len(cur['content'])} chars; editors: {', '.join(people)}")
        print()
    if not args.page:
        print("Show a page with: swarmy wiki RUN --page TITLE [--wiki ID]")
        if data.get("storage") == "git":
            print(f"Every edit is a git commit: git -C {data['repo']} log --format='%h %an: %s' -p")
    return 0


def _run_dir(args) -> Path:
    results, _ = _paths(args)
    runs_dir = results / "runs"
    if args.run in (None, "latest"):
        found = sorted(runs_dir.glob("run_*/run.log"), key=lambda f: f.stat().st_mtime)
        if not found:
            raise SystemExit(f"no run logs under {runs_dir}")
        return found[-1].parent
    run_dir = runs_dir / args.run
    if not run_dir.exists():
        raise SystemExit(f"no run {args.run} under {runs_dir}")
    return run_dir


def cmd_log(args) -> int:
    run_dir = _run_dir(args)
    f = run_dir / "run.log"
    if not f.exists():
        raise SystemExit(f"{run_dir.name} has no run.log (it ran before run logs were added)")
    keep = True
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if line[:2].isdigit():  # a new entry; continuation lines follow their entry
            keep = args.all or " DEBUG " not in line[:30]
        if keep:
            print(line)
    return 0


def cmd_ui(args) -> int:
    from .ui.server import serve

    results, _ = _paths(args)
    httpd = serve(args.host, args.port, Path(args.orgs_dir) if args.orgs_dir else None, results, args.models)
    host, port = httpd.server_address[:2]
    print(f"swarmy UI on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="swarmy", description=__doc__)
    ap.add_argument("--models", default=None, help="model catalog YAML (default: configs/models.yaml)")
    ap.add_argument("--results", default=str(DEFAULT_RESULTS_DIR), help="results directory (default: results)")
    ap.add_argument("--db", default=None, help="SQLite path (default: <results>/swarmy.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run one org on one task")
    p.add_argument("--org", required=True, help="org config YAML")
    p.add_argument("--task", help="benchmark task spec, e.g. qa:kind=chain,hops=3 | code:n=6 | mixed")
    p.add_argument("--mission", help="a free-form objective for the top of the org (ungraded)")
    p.add_argument("--mission-file", help="read the mission text from a file")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--budget", type=float, default=0.50, help="hard cap for this run in USD (default 0.50)")
    p.add_argument("--timeout", type=float, default=1200)
    p.add_argument("--mock", action="store_true", help="offline scripted models, no API calls")
    p.add_argument("--no-preflight", action="store_true",
                   help="skip the quick check of every model before the run (one tiny call per model)")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="print the run log as it happens (-v: orders, reports, errors; -vv: every call too)")
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

    p = sub.add_parser("orders", help="show how the mission cascaded down the chain of command")
    p.add_argument("run", nargs="?", default="latest", help="run id (default: latest)")
    p.add_argument("--full", action="store_true", help="print full order and report texts")
    p.set_defaults(fn=cmd_orders)

    p = sub.add_parser("wiki", help="browse the wikis a run produced, with who edited what")
    p.add_argument("run", nargs="?", default="latest", help="run id (default: the latest run)")
    p.add_argument("--wiki", help="only this wiki instance, e.g. unit-log/captain-1")
    p.add_argument("--page", help="print this page with its revision history")
    p.set_defaults(fn=cmd_wiki)

    p = sub.add_parser("log", help="show a run's debug log: calls, errors, check-ins and why it ended")
    p.add_argument("run", nargs="?", help="run id (default: latest)")
    p.add_argument("--all", action="store_true", help="include DEBUG lines (every model call and tool call)")
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("ui", help="open the interactive org designer in your browser")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--orgs-dir", default=None, help="where org YAML files are read and saved (default configs/orgs)")
    p.set_defaults(fn=cmd_ui)

    p = sub.add_parser("models", help="list models / endpoints, check live prices, or test a model")
    p.add_argument("action", nargs="?", default="list", choices=["list", "endpoints", "check", "refresh", "test"])
    p.add_argument("name", nargs="?", help="for `test`: a model alias or endpoint:model-id")
    p.set_defaults(fn=cmd_models)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
