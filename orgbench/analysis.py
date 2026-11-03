"""Turn the trace store into per-run metrics, per-org summaries, charts and a Markdown report."""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from .tasks import make_task
from .tracing import Tracer

# Categorical palette (validated for CVD separation on the light surface below).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SURFACE = "#fcfcfb"
TEXT, TEXT_2, MUTED = "#0b0b0b", "#52514e", "#a3a29a"
GRID, AXIS = "#e1e0d9", "#c3c2b7"


@lru_cache(maxsize=256)
def _key_terms(family: str, params_json: str, seed: int) -> tuple[str, ...]:
    try:
        return tuple(make_task(family, json.loads(params_json), seed).key_terms)
    except Exception:
        return ()


def run_metrics(run: dict, calls: list[dict], events: list[dict]) -> dict:
    total_cost = sum(c["cost_usd"] or 0 for c in calls)
    mgr = [c for c in calls if c["is_manager"]]
    tok = lambda c: (c["input_tokens"] or 0) + (c["cache_read_tokens"] or 0) + (c["cache_write_tokens"] or 0) + (c["output_tokens"] or 0)
    total_tok = sum(tok(c) for c in calls)
    by_level: dict[int, float] = defaultdict(float)
    for c in calls:
        by_level[c["level"]] += c["cost_usd"] or 0
    ev = [dict(e, data=json.loads(e["data_json"] or "{}")) for e in events]
    assigns = [e for e in ev if e["kind"] == "assign"]
    leaf_first = [e for e in assigns if e["data"].get("leaf") and e["data"].get("round") == 1]
    terms = _key_terms(run["family"], run["task_params"] or "{}", run["seed"])
    counts = Counter(t for e in leaf_first for t in e["data"].get("terms", []))
    coverage = redundancy = None
    if terms and run["depth"]:
        coverage = len([t for t in terms if counts.get(t)]) / len(terms)
        covered = [counts[t] for t in terms if counts.get(t)]
        redundancy = sum(covered) / len(covered) if covered else None
    root_progress = [e["data"]["score"] for e in ev if e["kind"] == "progress" and e["level"] == 0]
    first_pass = root_progress[0] if root_progress else None
    return {
        "coord_cost_share": (sum(c["cost_usd"] or 0 for c in mgr) / total_cost) if total_cost else None,
        "coord_token_share": (sum(tok(c) for c in mgr) / total_tok) if total_tok else None,
        "cost_by_level": dict(by_level),
        "delegations": len(assigns),
        "rework": len([e for e in assigns if e["data"].get("round", 1) > 1]),
        "mean_instruction_chars": statistics.mean([e["data"]["chars"] for e in assigns]) if assigns else None,
        "coverage": coverage,
        "redundancy": redundancy,
        "first_pass_score": first_pass,
        "review_gain": (run["score"] - first_pass) if first_pass is not None and run["score"] is not None else None,
        "nudges": len([e for e in ev if e["kind"] == "nudge"]),
        "turn_limits": len([e for e in ev if e["kind"] == "turn_limit"]),
        "agent_errors": len([e for e in ev if e["kind"] == "error"]),
        "tool_errors": len([e for e in ev if e["kind"] == "tool" and not e["data"].get("ok", True)]),
        "fallback_calls": sum(c["fallback"] or 0 for c in calls),
        "parallelism": (run["llm_s"] / run["wall_s"]) if run.get("wall_s") else None,
    }


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def _ci95(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    return 1.96 * statistics.stdev(xs) / math.sqrt(len(xs))


def load_sweep(tracer: Tracer, sweep_ref: str | None):
    sweep = tracer.resolve_sweep(sweep_ref)
    if sweep is None:
        raise SystemExit("no sweep found" + (f" matching {sweep_ref!r}" if sweep_ref else ""))
    runs = tracer.query("SELECT * FROM runs WHERE sweep_id=? ORDER BY started_at", (sweep["sweep_id"],))
    done = [r for r in runs if not (r["status"] or "").startswith("skipped")]
    ids = [r["run_id"] for r in done]
    calls, events = defaultdict(list), defaultdict(list)
    for rid in ids:
        calls[rid] = tracer.query("SELECT * FROM calls WHERE run_id=?", (rid,))
        events[rid] = tracer.query("SELECT * FROM events WHERE run_id=? ORDER BY id", (rid,))
    for r in done:
        r.update(run_metrics(r, calls[r["run_id"]], events[r["run_id"]]))
    return sweep, runs, done


def summarize(done: list[dict]) -> list[dict]:
    by_org: dict[str, list[dict]] = defaultdict(list)
    for r in done:
        by_org[r["org_name"]].append(r)
    out = []
    for org, rs in by_org.items():
        scores = [r["score"] for r in rs]
        cost = sum(r["cost_usd"] or 0 for r in rs)
        passes = sum(r["passed"] or 0 for r in rs)
        statuses = Counter(r["status"] for r in rs)
        out.append({
            "org": org, "shape": rs[0]["shape"], "depth": rs[0]["depth"], "n_agents": rs[0]["n_agents"],
            "n": len(rs), "score": _mean(scores), "score_ci95": _ci95(scores), "pass_rate": passes / len(rs),
            "cost_per_run": cost / len(rs), "total_cost": cost, "cost_per_pass": (cost / passes) if passes else None,
            "wall_s": _mean([r["wall_s"] for r in rs]),
            "coord_cost_share": _mean([r["coord_cost_share"] for r in rs]),
            "coord_token_share": _mean([r["coord_token_share"] for r in rs]),
            "delegations": _mean([r["delegations"] for r in rs]), "rework": _mean([r["rework"] for r in rs]),
            "coverage": _mean([r["coverage"] for r in rs]), "redundancy": _mean([r["redundancy"] for r in rs]),
            "review_gain": _mean([r["review_gain"] for r in rs]),
            "parallelism": _mean([r["parallelism"] for r in rs]),
            "statuses": dict(statuses),
            "families": {fam: _mean([r["score"] for r in rs if r["family"] == fam])
                         for fam in sorted({r["family"] for r in rs})},
            "cost_by_level": _sum_levels([r["cost_by_level"] for r in rs], len(rs)),
            "roles": json.loads(rs[0]["org_json"])["levels"],
        })
    out.sort(key=lambda s: (-(s["score"] or 0), s["cost_per_run"]))
    return out


def _sum_levels(dicts, n):
    acc = defaultdict(float)
    for d in dicts:
        for k, v in d.items():
            acc[int(k)] += v
    return {k: v / n for k, v in sorted(acc.items())}


def pareto(summary: list[dict]) -> list[str]:
    """Orgs not dominated on (higher mean score, lower mean cost)."""
    front, best = [], -1.0
    for s in sorted(summary, key=lambda s: (s["cost_per_run"], -(s["score"] or 0))):
        if (s["score"] or 0) > best:
            front.append(s["org"])
            best = s["score"] or 0
    return front


# ---------------------------------------------------------------------------
# Charts (static PNGs for the Markdown report)
# ---------------------------------------------------------------------------

def _style(ax, fig):
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=TEXT_2, labelsize=9)
    ax.xaxis.label.set_color(TEXT_2)
    ax.yaxis.label.set_color(TEXT_2)
    ax.title.set_color(TEXT)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def charts(summary: list[dict], front: list[str], out: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    files = []
    # 1. Quality vs cost with the Pareto frontier
    fig, ax = plt.subplots(figsize=(8, 5.2), dpi=150)
    _style(ax, fig)
    pts = sorted(summary, key=lambda s: s["cost_per_run"])
    fpts = [s for s in pts if s["org"] in front]
    if len(fpts) > 1:
        ax.plot([s["cost_per_run"] for s in fpts], [s["score"] or 0 for s in fpts], color=MUTED, linewidth=2,
                zorder=1, label="Pareto frontier")
    for s in pts:
        on = s["org"] in front
        ax.errorbar(s["cost_per_run"], s["score"] or 0, yerr=s["score_ci95"] or 0, fmt="o",
                    markersize=9, color=SERIES[0] if on else MUTED, ecolor=GRID, elinewidth=2, capsize=0,
                    markeredgecolor=SURFACE, markeredgewidth=2, zorder=3)
        ax.annotate(s["org"], (s["cost_per_run"], s["score"] or 0), textcoords="offset points", xytext=(7, 5),
                    fontsize=8, color=TEXT if on else TEXT_2)
    ax.set_xlabel("mean cost per run (USD)")
    ax.set_ylabel("mean score (0-1)")
    ax.set_ylim(-0.05, 1.08)
    ax.set_title("Quality vs cost (blue = on the Pareto frontier; bars = 95% CI)", fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(out / "pareto.png", facecolor=SURFACE)
    plt.close(fig)
    files.append("pareto.png")

    # 2. Where the money goes: cost per run by level
    max_levels = max((len(s["roles"]) for s in summary), default=1)
    fig, ax = plt.subplots(figsize=(8, 0.5 * len(summary) + 1.8), dpi=150)
    _style(ax, fig)
    ax.grid(axis="y", visible=False)
    names = [s["org"] for s in summary][::-1]
    lefts = [0.0] * len(names)
    rows = summary[::-1]
    for lvl in range(max_levels):
        widths = [s["cost_by_level"].get(lvl, 0.0) for s in rows]
        ax.barh(names, widths, left=lefts, color=SERIES[lvl % len(SERIES)], edgecolor=SURFACE, linewidth=2,
                height=0.6, label=f"level {lvl}" + (" (top)" if lvl == 0 else ""))
        lefts = [a + b for a, b in zip(lefts, widths)]
    ax.set_xlabel("mean cost per run (USD), split by level of the hierarchy")
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT_2, loc="upper left", bbox_to_anchor=(0, -0.16 - 0.6 / max(1, len(summary))),
              ncol=min(max_levels, 6))
    ax.set_title("Cost by level (level 0 = top; bottom level = workers)", fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(out / "cost_by_level.png", facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    files.append("cost_by_level.png")

    # 3. Score by task family (dot plot)
    fams = sorted({f for s in summary for f in s["families"]})
    if fams:
        fig, ax = plt.subplots(figsize=(8, 0.5 * len(summary) + 1.8), dpi=150)
        _style(ax, fig)
        ys = list(range(len(rows)))
        for i, fam in enumerate(fams):
            xs = [s["families"].get(fam) for s in rows]
            ax.scatter([x for x in xs if x is not None], [y for y, x in zip(ys, xs) if x is not None],
                       s=70, color=SERIES[i % len(SERIES)], edgecolors=SURFACE, linewidths=2, label=fam, zorder=3)
        ax.set_yticks(ys, names)
        ax.set_xlim(-0.05, 1.05)
        ax.set_xlabel("mean score")
        ax.legend(frameon=False, fontsize=8, labelcolor=TEXT_2, loc="upper left",
                  bbox_to_anchor=(0, -0.16 - 0.6 / max(1, len(summary))), ncol=len(fams))
        ax.set_title("Score by task family", fontsize=10, loc="left")
        fig.tight_layout()
        fig.savefig(out / "score_by_family.png", facecolor=SURFACE, bbox_inches="tight")
        plt.close(fig)
        files.append("score_by_family.png")
    return files


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _f(x, fmt="{:.2f}", none="-"):
    return none if x is None else fmt.format(x)


def write_report(tracer: Tracer, sweep_ref: str | None, out_dir: Path | None = None,
                 results_dir: Path = Path("results")) -> Path:
    sweep, runs, done = load_sweep(tracer, sweep_ref)
    out = out_dir or (results_dir / "reports" / f"{sweep['name']}-{sweep['sweep_id']}")
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize(done)
    front = pareto(summary)
    files = charts(summary, front, out) if summary else []

    run_cols = ["run_id", "org_name", "shape", "depth", "task_id", "family", "seed", "rep", "status", "score",
                "passed", "cost_usd", "wall_s", "n_calls", "input_tokens", "output_tokens", "cache_read_tokens",
                "reasoning_tokens", "rate_wait_s", "coord_cost_share", "coord_token_share", "delegations", "rework",
                "coverage", "redundancy", "first_pass_score", "review_gain", "nudges", "turn_limits",
                "agent_errors", "tool_errors", "fallback_calls"]
    with open(out / "runs.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=run_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(done)
    with open(out / "levels.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["run_id", "org", "level", "role", "model_alias", "calls", "input_tokens", "output_tokens",
                    "cache_read_tokens", "reasoning_tokens", "cost_usd", "latency_s", "rate_wait_s"])
        for r in done:
            for row in tracer.query(
                    "SELECT level, role, model_alias, COUNT(*) calls, SUM(input_tokens) i, SUM(output_tokens) o, "
                    "SUM(cache_read_tokens) cr, SUM(reasoning_tokens) rt, SUM(cost_usd) c, SUM(latency_s) l, "
                    "SUM(rate_wait_s) rw FROM calls WHERE run_id=? GROUP BY level, role, model_alias ORDER BY level",
                    (r["run_id"],)):
                w.writerow([r["run_id"], r["org_name"], row["level"], row["role"], row["model_alias"], row["calls"],
                            row["i"], row["o"], row["cr"], row["rt"], round(row["c"] or 0, 6),
                            round(row["l"] or 0, 2), round(row["rw"] or 0, 2)])

    skipped = Counter(r["status"] for r in runs if (r["status"] or "").startswith("skipped"))
    lines = [
        f"# Sweep report: {sweep['name']}",
        "",
        f"- sweep id: `{sweep['sweep_id']}`{'  (MOCK: offline, scores and costs are not meaningful)' if sweep['mock'] else ''}",
        f"- budget ${sweep['budget_usd']:.2f}, spent ${sweep['spent_usd'] or 0:.4f}",
        f"- runs completed: {len(done)}" + (f"; skipped: {dict(skipped)}" if skipped else ""),
        "",
        "## Organizations",
        "",
        "| org | shape | n | score (±95% CI) | pass | $/run | $/pass | wall s | coord $ | deleg | rework | coverage | redundancy | review gain |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summary:
        mark = " ★" if s["org"] in front else ""
        lines.append(
            f"| {s['org']}{mark} | {s['shape']} | {s['n']} | {_f(s['score'])} ± {_f(s['score_ci95'], none='?')} | "
            f"{_f(s['pass_rate'], '{:.0%}')} | {_f(s['cost_per_run'], '${:.4f}')} | {_f(s['cost_per_pass'], '${:.4f}')} | "
            f"{_f(s['wall_s'], '{:.0f}')} | {_f(s['coord_cost_share'], '{:.0%}')} | {_f(s['delegations'], '{:.1f}')} | "
            f"{_f(s['rework'], '{:.1f}')} | {_f(s['coverage'], '{:.0%}')} | {_f(s['redundancy'], '{:.2f}')} | "
            f"{_f(s['review_gain'], '{:+.2f}')} |")
    lines += ["", "★ = on the cost/quality Pareto frontier.", ""]
    fams = sorted({f for s in summary for f in s["families"]})
    if fams:
        lines += ["## Score by task family", "", "| org | " + " | ".join(fams) + " |",
                  "|---|" + "---|" * len(fams)]
        for s in summary:
            lines.append(f"| {s['org']} | " + " | ".join(_f(s["families"].get(f)) for f in fams) + " |")
        lines.append("")
    lines += ["## Cost by level (mean $/run)", "", "| org | " + " | ".join(f"L{i}" for i in range(6)) + " |",
              "|---|" + "---|" * 6]
    for s in summary:
        cells = []
        for i in range(6):
            if i < len(s["roles"]):
                cells.append(f"{s['roles'][i]['role']}: {_f(s['cost_by_level'].get(i, 0.0), '${:.4f}')}")
            else:
                cells.append("")
        lines.append(f"| {s['org']} | " + " | ".join(cells) + " |")
    lines += ["", "## Run outcomes", "", "| org | statuses |", "|---|---|"]
    for s in summary:
        lines.append(f"| {s['org']} | {', '.join(f'{k}: {v}' for k, v in sorted(s['statuses'].items()))} |")
    lines.append("")
    for f in files:
        lines += [f"![{f}]({f})", ""]
    lines += [
        "## Metric definitions",
        "",
        "- **score**: task grade in [0, 1] (QA: exact answer; code/mixed: fraction of hidden tests passed). "
        "**pass**: score = 1.",
        "- **coord $**: share of spend on manager nodes (everything above the workers).",
        "- **deleg / rework**: assignments per run / assignments to an agent that had already been assigned work once.",
        "- **coverage**: fraction of the task's key items (products, functions, cities...) that appear in at least "
        "one first-round worker assignment; low coverage means work got lost on the way down.",
        "- **redundancy**: average number of workers that received each covered item in the first round "
        "(1.0 = no duplicated work).",
        "- **review gain**: final score minus the workspace score when the top agent's first delegation returned "
        "(code/mixed only); positive means review and rework at the top helped.",
        "- With few runs per org, CIs are wide. Treat differences inside the CI as noise.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    (out / "summary.json").write_text(json.dumps({"sweep": sweep, "orgs": summary, "pareto": front},
                                                 indent=2, default=str))
    return out
