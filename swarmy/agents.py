"""Agent nodes and the per-agent tool loop."""

from __future__ import annotations

import time
import traceback
from typing import TYPE_CHECKING

from .catalog import Catalog, ModelSpec, Price
from .config import Node, NodeSettings, OrgConfig
from .prompts import NUDGE, system_prompt
from .errors import stops_run
from .providers import ProviderError, ToolResult
from .tools import tool_specs, validate_args

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
            self.wiki_writes, self.reminded = 0, False
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
        ctx.event(self, "agent_done", {"order_id": order_id, "status": result["status"], "turns": self.turns,
                                       "seconds": round(time.monotonic() - t0, 2)})
        ctx.live(self, "done", {"status": result["status"]})
        return result

    async def _handle(self, text: str, ctx: "RunContext") -> dict:
        provider = ctx.registry.provider_for(self.model)
        if self.conv is None or ctx.org.memory == "fresh":
            specs = tool_specs(self, ctx)
            self.specs = {s.name: s for s in specs}
            self.conv = provider.new_conversation(system_prompt(self, ctx), specs)
        self.conv.add_user(text)
        ctx.transcript.write(node=self.name, kind="input", text=text)

        nudges, last_text = 0, ""
        limit = self.max_turns(ctx.org)
        for turn in range(limit):
            self.turns = turn + 1
            self._deliver_inbox(ctx)
            max_tokens = ctx.budget.plan_call(self.model, self.conv.approx_input_tokens(), self.spec.output_cap())
            gate = ctx.registry.gate_for(self.model)
            queued = await gate.acquire()
            ctx.live(self, "thinking", {})
            try:
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
                if nudges < MAX_NUDGES:
                    nudges += 1
                    ctx.event(self, "nudge", {"stop": result.stop_reason})
                    self.conv.add_user(NUDGE)
                    continue
                ctx.event(self, "gave_up", {"nudges": nudges, "stop": result.stop_reason})
                return {"report": last_text or "[no report]", "answer": None, "status": "no_submit"}

            if result.stop_reason == "max_tokens":
                self.conv.add_tool_results([
                    ToolResult(c.id, "Not executed: your response hit the output-token limit. Retry with a "
                                     "shorter response.", True) for c in result.tool_calls])
                continue

            outputs: dict[str, ToolResult] = {}
            submission = None
            for call in sorted(result.tool_calls, key=lambda c: c.name == "submit"):
                if call.name == "submit" and call.error is None and "submit" in self.specs:
                    problem = validate_args(self.specs["submit"], call.arguments)
                    if problem:
                        outputs[call.id] = ToolResult(call.id, f"Submission rejected: {problem}", True)
                    elif self._should_remind(ctx):
                        self.reminded = True
                        ctx.event(self, "wiki_reminder", {})
                        writable = [w.id for w, can in ctx.wiki_access(self) if can]
                        outputs[call.id] = ToolResult(
                            call.id, "Not submitted yet. Your report only reaches your commander. If your work produced "
                            "facts, decisions, interfaces or results that others need, record them on the right "
                            f"topic page first ({', '.join(writable)}). Then call submit again; it will be accepted.")
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
                        "answer": None if answer is None else str(answer), "status": "submitted"}

        ctx.event(self, "turn_limit", {"turns": limit})
        return {"report": f"[turn limit reached] {last_text}", "answer": None, "status": "turn_limit"}


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
