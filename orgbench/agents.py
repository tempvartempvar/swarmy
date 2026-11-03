"""Agent nodes and the per-agent tool loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .catalog import Catalog, ModelSpec, Price
from .config import LevelSpec, OrgConfig
from .prompts import NUDGE, system_prompt
from .providers import ProviderError, ToolResult
from .tools import tool_specs, validate_args

if TYPE_CHECKING:
    from .runner import RunContext

MAX_NUDGES = 2


class Agent:
    def __init__(self, name: str, level: int, spec: LevelSpec, model: ModelSpec, parent: "Agent | None"):
        self.name = name
        self.level = level
        self.spec = spec
        self.model = model
        self.parent = parent
        self.children: list[Agent] = []
        self.conv = None
        self.specs: dict = {}

    def max_turns(self, org: OrgConfig) -> int:
        if self.spec.max_turns:
            return self.spec.max_turns
        return org.manager_max_turns if self.children else org.worker_max_turns

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    async def handle(self, text: str, ctx: "RunContext") -> dict:
        """Work on one assignment until `submit` (or a limit). Returns {report, answer, status}."""
        provider = ctx.registry.provider_for(self.model)
        if self.conv is None or ctx.org.memory == "fresh":
            specs = tool_specs(self, ctx)
            self.specs = {s.name: s for s in specs}
            self.conv = provider.new_conversation(system_prompt(self, ctx.org, ctx.task), specs)
        self.conv.add_user(text)
        ctx.transcript.write(node=self.name, kind="input", text=text)

        nudges, last_text = 0, ""
        for turn in range(self.max_turns(ctx.org)):
            max_tokens = ctx.budget.plan_call(self.model, self.conv.approx_input_tokens(), self.spec.output_cap())
            gate = ctx.registry.gate_for(self.model)
            queued = await gate.acquire()
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

        ctx.event(self, "turn_limit", {})
        return {"report": f"[turn limit reached] {last_text}", "answer": None, "status": "turn_limit"}


def _clip(args: dict, n: int = 1500) -> dict:
    out = {}
    for k, v in args.items():
        s = v if isinstance(v, str) else repr(v)
        out[k] = s if len(s) <= n else s[:n] + f"... [{len(s) - n} more chars]"
    return out


def build_tree(org: OrgConfig, catalog: Catalog, mock: bool = False) -> Agent:
    def resolve(level: LevelSpec) -> ModelSpec:
        spec = catalog.resolve(level.model)
        if mock and spec.provider != "mock":
            spec = spec.model_copy(update={"price": Price(input=0, output=0)})
        return spec

    resolved = [resolve(lvl) for lvl in org.levels]

    def make(level: int, parent: Agent | None, path: str) -> Agent:
        spec = org.levels[level]
        name = spec.role if level == 0 else f"{spec.role}-{path}"
        agent = Agent(name, level, spec, resolved[level], parent)
        if level < len(org.levels) - 1:
            for i in range(spec.span):
                agent.children.append(make(level + 1, agent, f"{path}.{i + 1}" if path else str(i + 1)))
        return agent

    return make(0, None, "")
