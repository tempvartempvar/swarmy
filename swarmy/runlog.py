"""The debug log of one run: `runs/<run_id>/run.log`.

One line per event, in order, with the time since the run started: every model call (tokens, cost,
latency, stop reason, retries and why), every order and report, failed tool calls, provider errors,
crashes with their tracebacks, check-ins, and finally why the run ended. Read it in the run
explorer's Log tab, with `swarmy log [RUN]`, or live with `swarmy run -v`.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

FORMAT = "%(asctime)s +%(elapsed)6.1fs %(levelname)-7s %(message)s"


class _Elapsed(logging.Filter):
    def __init__(self, t0: float):
        super().__init__()
        self.t0 = t0

    def filter(self, record: logging.LogRecord) -> bool:
        record.elapsed = record.created - self.t0
        return True


def open_run_log(run_id: str, path: Path, echo: int | None = None) -> logging.Logger:
    """A logger writing to `path` (and to stderr at level `echo`, if given)."""
    log = logging.getLogger(f"swarmy.run.{run_id}")
    log.setLevel(logging.DEBUG)
    log.propagate = False  # only the handlers below; never Python's last-resort stderr handler
    elapsed = _Elapsed(time.time())
    fmt = logging.Formatter(FORMAT, "%H:%M:%S")
    handlers: list[logging.Handler] = [logging.FileHandler(path, encoding="utf-8")]
    if echo is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
        handlers[-1].setLevel(echo)
    for h in handlers:
        h.addFilter(elapsed)
        h.setFormatter(fmt)
        log.addHandler(h)
    return log


def close_run_log(log: logging.Logger) -> None:
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)
    logging.Logger.manager.loggerDict.pop(log.name, None)


def _clip(text, n: int = 240) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= n else one[: n - 1] + "…"


def describe_event(node: str, kind: str, d: dict) -> tuple[int, str]:
    """(log level, message) for one tracer event."""
    I, W, E, D = logging.INFO, logging.WARNING, logging.ERROR, logging.DEBUG  # noqa: E741
    if kind == "mission":
        return I, f"mission -> {d.get('to')}: {_clip(d.get('title') or d.get('text'))}"
    if kind == "assign":
        fields = d.get("fields") or {}
        rework = f", rework round {d.get('round')}" if (d.get("round") or 1) > 1 else ""
        return I, (f"{node} -> {d.get('to')}: order #{d.get('order_id')} (part of #{d.get('parent_order')}{rework}): "
                   f"{_clip(fields.get('objective') or d.get('preview'))}")
    if kind == "report":
        lvl = I if d.get("status") == "submitted" else W
        return lvl, f"{node} -> {d.get('to')}: report on order #{d.get('order_id')} [{d.get('status')}]: {_clip(d.get('preview'))}"
    if kind == "tool":
        if d.get("ok"):
            return D, f"{node} {d.get('name')}: ok"
        return W, f"{node} {d.get('name')} failed: {_clip(d.get('error'), 400)}"
    if kind == "error":
        return E, f"{node} provider error: {d.get('error')}"
    if kind == "crash":
        return E, f"{node} crashed in {d.get('where')}: {d.get('error')}\n{d.get('traceback', '').rstrip()}"
    if kind == "turn_limit":
        return W, f"{node} used all {d.get('turns', '?')} of its turns without calling submit"
    if kind == "gave_up":
        return W, f"{node} stopped calling tools and never submitted (after {d.get('nudges')} reminders)"
    if kind == "nudge":
        return I, f"{node} answered in plain text (stop: {d.get('stop')}); reminded to use tools or submit"
    if kind == "refusal":
        return W, f"{node} declined the request"
    if kind == "agent_done":
        lvl = I if d.get("status") == "submitted" else W
        return lvl, (f"{node} finished order #{d.get('order_id')} [{d.get('status')}] after {d.get('turns')} turn(s), "
                     f"{d.get('seconds', 0):.1f}s")
    if kind == "checkin":
        tests = (d.get("tests") or "").strip().splitlines()
        shown = "".join(f"\n    {ln[:200]}" for ln in tests[:14]) + (f"\n    … {len(tests) - 14} more lines" if len(tests) > 14 else "")
        return W, (f"check-in {d.get('round')}/{d.get('max')} with {node}: {d.get('reason')}"
                   + (f"\n  public tests:{shown}" if tests else ""))
    if kind == "checkin_skipped":
        return W, f"no check-in: {d.get('reason')}"
    if kind == "wiki_write":
        return I, (f"{node} edited {d.get('wiki')} / {d.get('page')} v{d.get('version')} ({d.get('mode')}, "
                   f"{d.get('delta', 0):+d} chars){': ' + _clip(d.get('summary'), 160) if d.get('summary') else ''}")
    if kind == "wiki_reminder":
        return I, f"{node} tried to submit without recording anything on a wiki; reminded once"
    if kind in ("ask", "message"):
        verb = "asked" if kind == "ask" else "messaged"
        return I, f"{node} {verb} {d.get('to')}: {_clip(d.get('preview'), 200)}"
    if kind == "progress":
        return I, f"{node} got reports back; workspace score now {d.get('score')}"
    if kind == "undelivered":
        return W, f"{d.get('count')} inbox message(s) were never read: {d.get('by_node')}"
    if kind == "write":
        return D, f"{node} wrote {d.get('path')} ({d.get('chars')} chars)"
    return D, f"{node} {kind} {_clip(json.dumps(d, default=str), 300)}"


def describe_call(node: str, model: str, turn: int, r, cost: float, max_tokens: int) -> tuple[int, str]:
    u = r.usage
    tools = ", ".join(c.name for c in r.tool_calls) or "no tools"
    msg = (f"{node} call (turn {turn + 1}, {model}): {u.input_tokens + u.cache_read_tokens} in"
           f"{f' ({u.cache_read_tokens} cached)' if u.cache_read_tokens else ''} / {u.output_tokens} out "
           f"(max {max_tokens}) ${cost:.5f} {r.latency_s:.1f}s, stop {r.stop_reason} -> {tools}")
    if r.retries:
        msg += f"\n  after {r.retries} retr{'y' if r.retries == 1 else 'ies'}: " + "; ".join(
            _clip(e, 200) for e in getattr(r, "retry_errors", [])[-3:])
        return logging.WARNING, msg
    return logging.DEBUG, msg


def explain_end(status: str, error: str | None, root: str, root_status: str | None, checkins: int,
                budget: float | None, spent: float, timeout_s: float) -> str:
    """Why the run ended, in one or two plain sentences."""
    tried = f" after {checkins} check-in{'s' if checkins != 1 else ''}" if checkins else ""
    first = (error or "").strip().splitlines()[0][:400] if error else ""
    if status == "completed":
        return f"{root} submitted its final report{tried}."
    if status == "turn_limit":
        return f"{root} used all of its turns without submitting{tried} (raise manager_max_turns or the position's max_turns)."
    if status == "no_submit":
        return f"{root} stopped calling tools without submitting{tried}."
    if status == "refused":
        return f"{root} declined the request."
    if status == "budget_exhausted":
        cap = f"${budget:.2f}" if budget is not None else "the"
        return f"The run's {cap} budget ran out (spent ${spent:.4f}). {first}"
    if status == "timeout":
        return f"The run hit its {timeout_s:.0f}s time limit."
    if status == "fatal":
        if first.startswith("Preflight"):
            return (error or "").strip()[:1500]
        return f"The model endpoint refused the run, so it was stopped: {first}"
    if status == "error":
        if root_status == "error":
            return f"{root} could not continue{tried}: {first}"
        return f"A Python error stopped the run: {first} (full traceback in run.log)."
    return f"Run ended with status {status}{tried}. {first}".strip()
