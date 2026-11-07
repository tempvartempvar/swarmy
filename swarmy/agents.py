"""Agent nodes and the per-agent tool loop."""

from __future__ import annotations

import json
import re
import time
import traceback
from typing import TYPE_CHECKING

from .catalog import Catalog, ModelSpec, Price
from .config import Node, NodeSettings, OrgConfig
from .prompts import NUDGE, system_prompt
from .errors import stops_run
from .providers import ProviderError, ToolCall, ToolResult
from .tools import STATUS_FALLBACK, STATUS_WORDS, tool_specs, validate_args

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
        self.current_order: int | None = None  # id of the order being worked on
        self.current_order_text: str = ""  # its text (used to stop verbatim forwarding)
        self.wiki_writes = 0  # wiki edits during the current assignment
        self.reminded = False
        self.status_warned = False  # told once per order to give a short status
        self.turns = 0  # model calls during the current assignment

    def max_turns(self, org: OrgConfig) -> int:
        if self.spec.max_turns:
            return self.spec.max_turns
        return org.manager_max_turns if self.children else org.worker_max_turns

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def _deliver_inbox(self, ctx: "RunContext") -> None:
        pending = ctx.inboxes.pop(self.name, [])
        if pending:
            self.conv.add_user("Messages received:\n" + "\n\n".join(pending))
            ctx.event(self, "inbox", {"count": len(pending)})

    async def handle(self, text: str, ctx: "RunContext", order_id: int | None = None) -> dict:
        """Work on one assignment until `submit` (or a limit). Returns {report, answer, status}."""
        if order_id is None or order_id != self.current_order:  # a new order (a check-in continues the same one)
            self.wiki_writes, self.reminded, self.status_warned = 0, False, False
            self.current_order_text = text
        self.current_order = order_id
        self.turns = 0
        ctx.active.add(self.name)
        ctx.live(self, "working", {})
        t0 = time.monotonic()
        try:
            result = await self._handle(text, ctx)
        except Exception as e:
            if stops_run(e):
                raise
            # A bug or unexpected response: report it to the commander instead of ending the whole run.
            tb = "".join(traceback.format_exception(e))
            ctx.event(self, "crash", {"where": "agent loop", "error": f"{type(e).__name__}: {e}"[:500],
                                      "traceback": tb[-4000:]})
            result = {"report": f"[{self.name} stopped: internal error {type(e).__name__}: {str(e)[:300]}]",
                      "answer": None, "status": "error"}
        finally:
            ctx.active.discard(self.name)
        result.setdefault("status_line", STATUS_FALLBACK.get(result["status"], result["status"]))
        ctx.event(self, "agent_done", {"order_id": order_id, "status": result["status"], "turns": self.turns,
                                       "seconds": round(time.monotonic() - t0, 2), "status_line": result["status_line"]})
        ctx.live(self, "done", {"status": result["status"], "line": result["status_line"]})
        return result

    def _status_problem(self, args: dict) -> str | None:
        """Check the submit `status` (at most STATUS_WORDS words). Rejects once per order with an example;
        after that it keeps the first words, so a cosmetic field never blocks the work."""
        words = str(args.get("status") or "").split()
        if words and len(words) <= STATUS_WORDS:
            args["status"] = " ".join(words)
            return None
        if not self.status_warned:
            self.status_warned = True
            got = f"yours has {len(words)} words" if words else "it is missing"
            return (f"Submission rejected: `status` must be a status update of at most {STATUS_WORDS} words for the "
                    f"org chart ({got}), e.g. 'Simulator matches all sample cases'. Call submit again with it.")
        args["status"] = " ".join(words[:STATUS_WORDS]) or "Done"

    async def _handle(self, text: str, ctx: "RunContext") -> dict:
        provider = ctx.registry.provider_for(self.model)
        if self.conv is None or ctx.org.memory == "fresh":
            specs = tool_specs(self, ctx)
            self.specs = {s.name: s for s in specs}
            self.conv = provider.new_conversation(system_prompt(self, ctx), specs)
        self.conv.add_user(text)
        ctx.transcript.write(node=self.name, kind="input", text=text)

        nudges, last_text, force_tool = 0, "", False
        limit = self.max_turns(ctx.org)
        for turn in range(limit):
            self.turns = turn + 1
            self._deliver_inbox(ctx)
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
            except ProviderError as e:
                ctx.event(self, "error", {"error": str(e)[:500]})
                ctx.transcript.write(node=self.name, kind="error", error=str(e))
                return {"report": f"[{self.name} could not continue: provider error: {e}]", "answer": None,
                        "status": "error"}
            finally:
                gate.release()
            result.rate_wait_s += queued
            cost = self.model.cost(result.usage)
            ctx.budget.charge(cost)
            ctx.log_call(self, result, cost, turn, max_tokens)
            ctx.live(self, "working", {"tools": [c.name for c in result.tool_calls]})
            ctx.transcript.write(
                node=self.name, kind="step", turn=turn, text=result.text, thinking=result.thinking[:4000],
                tool_calls=[{"name": c.name, "args": _clip(c.arguments)} for c in result.tool_calls],
                stop=result.stop_reason, cost=round(cost, 6),
            )
            if result.text:
                last_text = result.text
            if result.stop_reason == "refusal":
                ctx.event(self, "refusal", {})
                return {"report": f"[{self.name} declined the request]", "answer": None, "status": "refused"}

            if not result.tool_calls:
                salvaged = self._salvage_submit(result.text)
                if salvaged is not None:
                    problem = self._submit_problem(salvaged, ctx)
                    ctx.event(self, "salvaged", {"tool": "submit", "accepted": problem is None,
                                                 "problem": (problem or "")[:300]})
                    if problem is None:
                        return {"report": str(salvaged.get("report") or salvaged.get("answer") or ""),
                                "answer": None if salvaged.get("answer") is None else str(salvaged["answer"]),
                                "status": "submitted", "status_line": salvaged["status"]}
                if nudges < MAX_NUDGES:
                    nudges += 1
                    force_tool = True
                    ctx.event(self, "nudge", {"stop": result.stop_reason})
                    self.conv.add_user(NUDGE if salvaged is None else
                                       f"Your submission was written as text, so it was not delivered: {problem} "
                                       "Call the `submit` tool itself (a tool call, not text).")
                    continue
                ctx.event(self, "gave_up", {"nudges": nudges, "stop": result.stop_reason})
                return {"report": last_text or "[no report]", "answer": None, "status": "no_submit"}

            force_tool = False
            if result.stop_reason == "max_tokens":
                self.conv.add_tool_results([
                    ToolResult(c.id, "Not executed: your response hit the output-token limit. Retry with a "
                                     "shorter response.", True) for c in result.tool_calls])
                continue

            outputs: dict[str, ToolResult] = {}
            submission = None
            for call in sorted(result.tool_calls, key=lambda c: c.name == "submit"):
                if call.name == "submit" and call.error is None and "submit" in self.specs:
                    problem = self._submit_problem(call.arguments, ctx)
                    if problem:
                        outputs[call.id] = ToolResult(call.id, problem, not problem.startswith("Not submitted yet"))
                    else:
                        submission = call.arguments
                        outputs[call.id] = ToolResult(call.id, "Submitted.")
                else:
                    outputs[call.id] = await ctx.tools.execute(self, call, self.specs)
            self.conv.add_tool_results([outputs[c.id] for c in result.tool_calls])
            for c in result.tool_calls:
                r = outputs[c.id]
                ctx.transcript.write(node=self.name, kind="tool_result", tool=c.name, error=r.is_error,
                                     content=r.content[:3000])
            if submission is not None:
                answer = submission.get("answer")
                return {"report": str(submission.get("report") or answer or ""),
                        "answer": None if answer is None else str(answer), "status": "submitted",
                        "status_line": submission["status"]}

        ctx.event(self, "turn_limit", {"turns": limit})
        return {"report": f"[turn limit reached] {last_text}", "answer": None, "status": "turn_limit"}


    def _submit_problem(self, args: dict, ctx: "RunContext") -> str | None:
        """Why a submit can't be accepted yet (None if it can): bad arguments, the status rule, or the
        one-time reminder to record findings on a wiki first."""
        problem = validate_args(self.specs["submit"], {**args, "status": "x"}) or self._status_problem(args)
        if problem:
            return f"Submission rejected: {problem}"
        if self._should_remind(ctx):
            self.reminded = True
            ctx.event(self, "wiki_reminder", {})
            writable = [w.id for w, can in ctx.wiki_access(self) if can]
            return ("Not submitted yet. Your report only reaches your commander. If your work produced facts, "
                    "decisions, interfaces or results that others need, and your orders don't say otherwise, record "
                    f"them on the right topic page first ({', '.join(writable)}). Then call submit again; it will "
                    "be accepted.")
        return None

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

    def _should_remind(self, ctx: "RunContext") -> bool:
        if self.reminded or self.wiki_writes or not ctx.org.knowledge.remind_before_submit:
            return False
        return any(can for _, can in ctx.wiki_access(self))


def _clip(args: dict, n: int = 1500) -> dict:
    out = {}
    for k, v in args.items():
        s = v if isinstance(v, str) else repr(v)
        out[k] = s if len(s) <= n else s[:n] + f"... [{len(s) - n} more chars]"
    return out


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
