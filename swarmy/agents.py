"""Agent nodes and the per-agent tool loop."""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
from typing import TYPE_CHECKING

from .catalog import Catalog, ModelSpec, Price
from .config import Node, NodeSettings, OrgConfig
from .errors import stops_run
from .prompts import NUDGE, system_prompt
from .providers import ProviderError, StepResult, ToolCall, ToolResult
from .providers.base import clip_args
from .tools import LIMIT_GRACE, STATUS_FALLBACK, STATUS_WORDS, tool_specs, validate_args

if TYPE_CHECKING:
    from .runner import RunContext

MAX_NUDGES = 2


class Agent:
    def __init__(self, node: Node, model: ModelSpec, parent: "Agent | None"):
        self.node = node
        self.name = node.name
        self.level = node.depth
        self.spec: NodeSettings = node.settings
        self.model = model
        self.parent = parent
        self.children: list[Agent] = []
        self.conv = None
        self.specs: dict = {}
        # the order it is working on
        self.current_order: int | None = None
        self.current_order_text = ""  # its text (used to stop verbatim forwarding)
        self.turns = 0  # model calls on it so far
        self.final_turn = False  # on its last turn: a submit is accepted as it is
        self.warned: set[str] = set()  # one-time submit warnings already given: status, report, wiki
        self.recorded = False  # wrote a wiki page or a workspace file for it
        # the orders it has out
        self.outstanding: dict[int, dict] = {}  # order id -> {"to", "task"} for orders still being worked on
        self.arrived: list[dict] = []  # reports back from subordinates, not yet shown to this agent
        self.report_signal = asyncio.Event()  # set whenever a report or a message arrives
        self.message_waiting = False  # a message arrived that should wake this agent from `wait`
        self.failure: BaseException | None = None  # a run-stopping error raised under one of its orders

    def max_turns(self, org: OrgConfig) -> int:
        if self.spec.max_turns:
            return self.spec.max_turns
        return org.manager_max_turns if self.children else org.worker_max_turns

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def take_messages(self, ctx: "RunContext") -> str:
        pending = ctx.inboxes.pop(self.name, [])
        self.message_waiting = False
        if not pending:
            return ""
        ctx.event(self, "inbox", {"count": len(pending)})
        return "Messages received:\n" + "\n\n".join(pending)

    def _deliver_inbox(self, ctx: "RunContext") -> None:
        self.raise_failure()
        messages = self.take_messages(ctx)
        if messages:
            self.conv.add_user(messages)
        if self.arrived:  # reports that came back while this agent was busy
            reps, self.arrived = self.arrived, []
            self.conv.add_user("Reports received:\n\n" + "\n\n".join(r["text"] for r in reps) + self.outstanding_note())

    # ---- orders out ----
    def raise_failure(self) -> None:
        if self.failure is not None:
            raise self.failure

    def outstanding_list(self) -> str:
        return ", ".join(f"#{oid} ({o['to']})" for oid, o in self.outstanding.items())

    def outstanding_note(self) -> str:
        if not self.outstanding:
            return "\n\nAll your orders have reported back." if self.children else ""
        return f"\n\nStill out: {self.outstanding_list()}."

    async def wait_for(self, done) -> None:
        """Sleep until done() is true, waking whenever a report arrives."""
        while not done():
            self.report_signal.clear()
            await self.report_signal.wait()
            self.raise_failure()

    def _cancel_outstanding(self) -> None:
        for o in self.outstanding.values():
            o["task"].cancel()

    async def _collect_late(self) -> str:
        """Orders still out when this agent stops: let them finish and pass their reports up."""
        await self.wait_for(lambda: not self.outstanding)
        reps, self.arrived = self.arrived, []
        if not reps:
            return ""
        return "\n\nReports from my subordinates that came back after I stopped:\n\n" + "\n\n".join(r["text"] for r in reps)

    # ---- working on an order ----
    async def handle(self, text: str, ctx: "RunContext", order_id: int | None = None) -> dict:
        """Work on one assignment until `submit` (or a limit). Returns {report, answer, status, status_line}."""
        if order_id is None or order_id != self.current_order:  # a new order (a check-in continues the same one)
            self.warned, self.recorded, self.current_order_text = set(), False, text
        self.current_order = order_id
        self.turns = 0
        ctx.active.add(self.name)
        ctx.live(self, "working", {})
        t0 = time.monotonic()
        try:
            result = await self._handle(text, ctx)
        except asyncio.CancelledError:
            self._cancel_outstanding()
            raise
        except Exception as e:
            if stops_run(e):
                self._cancel_outstanding()
                raise
            # A bug or unexpected response: report it to the commander instead of ending the whole run.
            tb = "".join(traceback.format_exception(e))
            ctx.event(self, "crash", {"where": "agent loop", "error": f"{type(e).__name__}: {e}"[:500],
                                      "traceback": tb[-4000:]})
            result = _result(f"[{self.name} stopped: internal error {type(e).__name__}: {str(e)[:300]}]", "error")
        finally:
            ctx.active.discard(self.name)
        if self.outstanding or self.arrived:
            result["report"] = str(result["report"]) + await self._collect_late()
        result.setdefault("status_line", STATUS_FALLBACK.get(result["status"], result["status"]))
        ctx.event(self, "agent_done", {"order_id": order_id, "status": result["status"], "turns": self.turns,
                                       "seconds": round(time.monotonic() - t0, 2), "status_line": result["status_line"]})
        ctx.live(self, "done", {"status": result["status"], "line": result["status_line"]})
        return result

    async def _handle(self, text: str, ctx: "RunContext") -> dict:
        provider = ctx.registry.provider_for(self.model)
        if self.conv is None:
            specs = tool_specs(self, ctx)
            self.specs = {s.name: s for s in specs}
            self.conv = provider.new_conversation(system_prompt(self, ctx), specs)
        self.conv.add_user(text)
        ctx.transcript.write(node=self.name, kind="input", text=text)

        nudges, last_text, force_tool = 0, "", False
        limit = self.max_turns(ctx.org)
        budget, turn = limit, -1
        while turn + 1 < budget:
            turn += 1
            self.turns = turn + 1
            self._deliver_inbox(ctx)
            self._trim_context(ctx)
            self._turns_left(budget - turn, limit)
            try:
                result, max_tokens = await self._step(ctx, provider, turn, force_tool)
            except ProviderError as e:
                ctx.event(self, "error", {"error": str(e)[:500]})
                ctx.transcript.write(node=self.name, kind="error", error=str(e))
                return _result(f"[{self.name} could not continue: provider error: {e}]", "error")
            last_text = result.text or last_text
            if result.stop_reason == "refusal":
                ctx.event(self, "refusal", {})
                return _result(f"[{self.name} declined the request]", "refused")

            if not result.tool_calls:  # plain text: a submit written as text, or a reminder to use tools
                salvaged = self._salvage_submit(result.text)
                problem = None if salvaged is None else self._submit_problem(salvaged, ctx)
                if salvaged is not None:
                    ctx.event(self, "salvaged", {"tool": "submit", "accepted": problem is None,
                                                 "problem": (problem or "")[:300]})
                    if problem is None:
                        return _submitted(salvaged)
                if nudges >= MAX_NUDGES:
                    ctx.event(self, "gave_up", {"nudges": nudges, "stop": result.stop_reason})
                    return _result(last_text or "[no report]", "no_submit")
                nudges, force_tool = nudges + 1, True
                ctx.event(self, "nudge", {"stop": result.stop_reason})
                self.conv.add_user(_nudge(salvaged, problem, result.stop_reason, max_tokens))
                continue

            force_tool = False
            if result.stop_reason == "max_tokens":
                self.conv.add_tool_results([
                    ToolResult(c.id, "Not executed: your response hit the output-token limit. Retry with a "
                                     "shorter response.", True) for c in result.tool_calls])
                continue
            submission, only_waited = await self._run_tools(result.tool_calls, ctx)
            if submission is not None:
                return _submitted(submission)
            if only_waited and budget < 2 * limit:
                budget += 1  # waiting for reports doesn't use up the turn budget

        ctx.event(self, "turn_limit", {"turns": limit})
        return _result(f"[turn limit reached] {last_text}", "turn_limit")

    def _turns_left(self, left: int, limit: int) -> None:
        self.final_turn = left == 1
        if left == 3 and limit > 4:
            self.conv.add_user("You have 3 turns left for this order. Wrap up: "
                               + ("collect your outstanding reports with `wait`, then " if self.outstanding else "")
                               + "call `submit` with your report, saying what is unfinished.")
        elif left == 1:
            self.conv.add_user("This is your last turn for this order: call `submit` now with what you have"
                               + (" (reports from orders still out will be passed up with yours)" if self.outstanding else "")
                               + ", or the order ends as 'ran out of turns'.")

    async def _step(self, ctx: "RunContext", provider, turn: int, force_tool: bool) -> tuple[StepResult, int]:
        """One model call: budgeted, rate-limited, charged and logged. Returns (result, max_tokens)."""
        max_tokens = ctx.budget.plan_call(self.model, self.conv.approx_input_tokens(), self.spec.output_cap())
        gate = ctx.registry.gate_for(self.model)
        queued = await gate.acquire()
        ctx.live(self, "thinking", {})
        try:
            # after a reminder, OpenAI-compatible endpoints are told a tool call is required
            choice = "required" if force_tool and self.model.kind == "openai" else None
            try:
                result = await provider.step(self.conv, self.model, max_tokens=max_tokens, effort=self.spec.effort,
                                             tool_choice=choice)
            except ProviderError:
                if choice is None:
                    raise
                result = await provider.step(self.conv, self.model, max_tokens=max_tokens, effort=self.spec.effort)
        finally:
            gate.release()
        result.rate_wait_s += queued
        cost = self.model.cost(result.usage)
        ctx.budget.charge(cost)
        ctx.log_call(self, result, cost, turn, max_tokens)
        ctx.live(self, "working", {"tools": [c.name for c in result.tool_calls]})
        ctx.transcript.write(
            node=self.name, kind="step", turn=turn, text=result.text, thinking=result.thinking[:4000],
            tool_calls=[{"name": c.name, "args": clip_args(c.arguments, 1500)} for c in result.tool_calls],
            stop=result.stop_reason, cost=round(cost, 6),
        )
        return result, max_tokens

    async def _run_tools(self, calls: list[ToolCall], ctx: "RunContext") -> tuple[dict | None, bool]:
        """Run one turn's tool calls, submit last. Returns (the accepted submit's arguments or None,
        whether the turn only waited for reports)."""
        outputs: dict[str, ToolResult] = {}
        submission = None
        for call in sorted(calls, key=lambda c: c.name == "submit"):
            if call.name == "submit" and call.error is None and "submit" in self.specs:
                problem = self._submit_problem(call.arguments, ctx)
                if problem:
                    outputs[call.id] = ToolResult(call.id, problem, not problem.startswith("Not submitted yet"))
                else:
                    submission = call.arguments
                    outputs[call.id] = ToolResult(call.id, "Submitted.")
            else:
                outputs[call.id] = await ctx.tools.execute(self, call, self.specs)
        self.conv.add_tool_results([outputs[c.id] for c in calls])
        for c in calls:
            ctx.transcript.write(node=self.name, kind="tool_result", tool=c.name, error=outputs[c.id].is_error,
                                 content=outputs[c.id].content[:3000])
        return submission, all(c.name == "wait" and not outputs[c.id].is_error for c in calls)

    def _trim_context(self, ctx: "RunContext") -> None:
        """Over the context budget: clip older material, keeping fewer recent turns intact if needed, down
        to about 60% of the budget so the next trim (and the next cache miss) is several turns away."""
        rules = ctx.org.context
        before = self.conv.approx_input_tokens()
        if before <= rules.max_tokens:
            return
        keep, saved = rules.keep_turns, 0
        while True:
            saved += self.conv.trim(keep, rules.trim_to)
            if self.conv.approx_input_tokens() <= 0.6 * rules.max_tokens or keep == 1:
                break
            keep -= 1
        if saved:
            ctx.event(self, "trimmed", {"before": before, "after": self.conv.approx_input_tokens(), "kept_turns": keep})

    # ---- submitting ----
    def _submit_problem(self, args: dict, ctx: "RunContext") -> str | None:
        """Why a submit can't be accepted yet (None if it can): orders still out, bad arguments, the status
        or report rule, or the one-time reminder to record findings on a wiki first. On the last turn only
        bad arguments stop it."""
        if self.outstanding and not self.final_turn:
            return (f"Submission rejected: some of your orders are still out ({self.outstanding_list()}). "
                    "Call `wait` to collect their reports first.")
        problem = (validate_args(self.specs["submit"], {**args, "status": "x"}) or self._status_problem(args)
                   or self._report_problem(args, ctx))
        if problem:
            return f"Submission rejected: {problem}"
        if self._should_remind(ctx):
            self.warned.add("wiki")
            ctx.event(self, "wiki_reminder", {})
            writable = [w.id for w, can in ctx.wiki_access(self) if can]
            return ("Not submitted yet. Your report only reaches your commander. If your work produced facts, "
                    "decisions, interfaces or results that others need, and your orders don't say otherwise, record "
                    f"them on the right topic page first ({', '.join(writable)}). Then call submit again; it will "
                    "be accepted.")
        return None

    def _warn_once(self, rule: str) -> bool:
        """True the first time `rule` is broken on this order (not on the last turn: it would cost the submit)."""
        if rule in self.warned or self.final_turn:
            return False
        self.warned.add(rule)
        return True

    def _status_problem(self, args: dict) -> str | None:
        """The `status` is at most STATUS_WORDS words. Rejected once per order with an example; after that the
        first words are kept, so a cosmetic field never blocks the work."""
        words = str(args.get("status") or "").split()
        if words and len(words) <= STATUS_WORDS:
            args["status"] = " ".join(words)
            return None
        if self._warn_once("status"):
            got = f"yours has {len(words)} words" if words else "it is missing"
            return (f"`status` must be a status update of at most {STATUS_WORDS} words for the org chart ({got}), "
                    "e.g. 'Simulator matches all sample cases'. Call submit again with it.")
        args["status"] = " ".join(words[:STATUS_WORDS]) or "Done"
        return None

    def _report_problem(self, args: dict, ctx: "RunContext") -> str | None:
        """Reports have a length limit too: rejected once per order with advice, then cut to the limit."""
        limit = ctx.org.orders.max_report_chars
        report = str(args.get("report") or "")
        if len(report) <= limit * LIMIT_GRACE:
            return None
        if self._warn_once("report"):
            return (f"your report is {len(report)} characters; keep it under {limit}. Report results, decisions "
                    "and open issues; put details in a file or wiki page and point to it.")
        args["report"] = report[:limit] + f"\n[... {len(report) - limit} characters cut: over the report limit]"
        return None

    def _should_remind(self, ctx: "RunContext") -> bool:
        # not when the results are already in a file or on a wiki, and only once per order
        if self.recorded or "wiki" in self.warned or self.final_turn:
            return False
        return any(can for _, can in ctx.wiki_access(self))

    def _salvage_submit(self, text: str) -> dict | None:
        """Some models (gpt-oss, for one) sometimes write the submit call as text, e.g.
        {"report": "...", "status": "..."} or {"name": "submit", "arguments": {...}}, possibly in a code
        block. Recover those arguments; anything else is left alone."""
        if "submit" not in self.specs or "{" not in (text or ""):
            return None
        props = set(self.specs["submit"].parameters.get("properties", {}))
        decoder = json.JSONDecoder()
        for m in re.finditer(r"\{", text):
            try:
                obj, _ = decoder.raw_decode(text[m.start():])
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            name = obj.get("name") or obj.get("tool") or obj.get("function")
            args = obj.get("arguments") or obj.get("parameters") or obj.get("args")
            if name == "submit" and isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = None
            if name == "submit" and isinstance(args, dict):
                return args
            if obj and set(obj) <= props and ("report" in obj or "answer" in obj):
                return obj
            return None  # the first JSON object in the text isn't a submit: leave it
        return None


def _result(report: str, status: str) -> dict:
    return {"report": report, "answer": None, "status": status}


def _submitted(args: dict) -> dict:
    answer = args.get("answer")
    return {"report": str(args.get("report") or answer or ""), "answer": None if answer is None else str(answer),
            "status": "submitted", "status_line": args["status"]}


def _nudge(salvaged: dict | None, problem: str | None, stop_reason: str, max_tokens: int) -> str:
    """What to tell an agent that replied in plain text."""
    if salvaged is not None:
        return (f"Your submission was written as text, so it was not delivered: {problem} "
                "Call the `submit` tool itself (a tool call, not text).")
    if stop_reason == "max_tokens":
        return (f"Your reply hit the output limit ({max_tokens} tokens) before you called a tool, so nothing "
                "happened. Keep your reasoning short and act now: call one tool. Write long content to files in "
                "parts if needed.")
    return NUDGE


def build_tree(org: OrgConfig, catalog: Catalog, mock: bool = False) -> Agent:
    resolved: dict[str, ModelSpec] = {}

    def resolve(name: str) -> ModelSpec:
        if name not in resolved:
            spec = catalog.resolve(name)
            if mock and spec.kind != "mock":
                spec = spec.model_copy(update={"price": Price(input=0, output=0)})
            resolved[name] = spec
        return resolved[name]

    def make(node: Node, parent: Agent | None) -> Agent:
        agent = Agent(node, resolve(node.settings.model), parent)
        agent.children = [make(c, agent) for c in node.children]
        return agent

    return make(org.root, None)
