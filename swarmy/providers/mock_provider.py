"""Offline scripted provider. Exercises the whole pipeline (delegation, tools, wikis, messaging,
logging, grading, budgets) at zero cost. It does not try to solve tasks: everything it writes is a
labelled placeholder ("[mock] ..."), never real planning."""

from __future__ import annotations

import asyncio
import itertools
import json
import re

from ..catalog import ModelSpec
from .base import Conversation, Provider, StepResult, ToolCall, ToolResult, ToolSpec, Usage

_ids = itertools.count(1)


class MockConversation(Conversation):
    def __init__(self, system: str, tools: list[ToolSpec]):
        super().__init__(system, tools)
        self.tools = {t.name: t for t in tools}
        self.events: list[tuple[str, object]] = []

    def add_user(self, text: str) -> None:
        self.chars += len(text)
        self.events.append(("user", text))

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            self.chars += len(r.content)
        self.events.append(("tool", results))

    def fork(self) -> "MockConversation":
        new = MockConversation(self.system, self.tool_specs)
        new.events = list(self.events)
        new.chars = self.chars
        return new

    def episode(self) -> tuple[str, list[str], list[ToolResult]]:
        """Latest assignment, tools used since then, and the latest tool results."""
        instruction, used, last_results = "", [], []
        for kind, payload in self.events:
            if kind == "user" and str(payload).startswith(("Assignment from", "Orders from", "[Side conversation]")):
                instruction, used, last_results = str(payload), [], []
            elif kind == "assistant":
                used.extend(c.name for c in payload)
            elif kind == "tool":
                last_results = payload
        return instruction, used, last_results


class MockProvider(Provider):
    name = "mock"

    def new_conversation(self, system: str, tools: list[ToolSpec]) -> MockConversation:
        return MockConversation(system, tools)

    async def step(self, conv, spec: ModelSpec, *, max_tokens: int, effort: str | None,
                   tool_choice: str | None = None) -> StepResult:
        assert isinstance(conv, MockConversation)
        await asyncio.sleep(0.002)
        instruction, used, last = conv.episode()
        if instruction.startswith("[Side conversation]") or tool_choice == "none":
            text = "[mock] Placeholder answer: proceed with the plan in your orders."
            conv.events.append(("assistant", []))
            conv.chars += len(text)
            return StepResult(text=text, tool_calls=[], usage=Usage(input_tokens=conv.chars // 4, output_tokens=20),
                              stop_reason="end_turn", latency_s=0.002, served_model="mock")
        call = self._policy(conv, instruction, used, last)
        conv.events.append(("assistant", [call]))
        out_tokens = 30 + len(json.dumps(call.arguments)) // 4
        conv.chars += out_tokens * 4
        return StepResult(
            text="",
            tool_calls=[call],
            usage=Usage(input_tokens=conv.chars // 4, output_tokens=out_tokens),
            stop_reason="tool_use",
            latency_s=0.002,
            served_model="mock",
        )

    @staticmethod
    def _enum(tool: ToolSpec, *path: str) -> list[str]:
        node = tool.parameters
        for key in path:
            node = node[key]
        return node.get("enum", [])

    def _policy(self, conv: MockConversation, instruction: str, used: list[str], last: list[ToolResult]) -> ToolCall:
        tools = conv.tools
        cid = f"mock_{next(_ids)}"
        me = (re.search(r"You are ([^,]+),", conv.system) or [None, "agent"])[1]
        if "wiki_write" in tools and "wiki_write" not in used:
            wiki = self._enum(tools["wiki_write"], "properties", "wiki")[0]
            return ToolCall(cid, "wiki_write", {
                "wiki": wiki, "page": "Plan", "append": True, "summary": f"[mock] placeholder note from {me}",
                "content": f"[mock] Placeholder note from {me}. Scripted mock agents don't plan or research; "
                           f"run with real models to see real notes."})
        if "delegate" in tools and "delegate" not in used:
            item = tools["delegate"].parameters["properties"]["assignments"]["items"]
            names = item["properties"]["to"].get("enum", [])
            structured = "objective" in item["properties"]
            assignments = []
            for i, n in enumerate(names):
                if structured:
                    assignments.append({"to": n, "objective": f"[mock] part {i + 1} of {len(names)} from {me}",
                                        "context": "[mock] Placeholder order: scripted agents don't distill orders.",
                                        "deliverables": "[mock] a report", "done_when": "[mock] after a few tool calls"})
                else:
                    assignments.append({"to": n, "task": f"[mock] part {i + 1} of {len(names)} from {me}"})
            return ToolCall(cid, "delegate", {"assignments": assignments})
        if "ask" in tools and "ask" not in used:
            to = self._enum(tools["ask"], "properties", "to")[0]
            return ToolCall(cid, "ask", {"to": to, "question": "[mock] placeholder question"})
        if "send_message" in tools and "send_message" not in used:
            to = self._enum(tools["send_message"], "properties", "to")[-1]
            return ToolCall(cid, "send_message", {"to": to, "text": "[mock] placeholder status message"})
        if "search_docs" in tools and "search_docs" not in used:
            words = re.findall(r"[A-Z][a-z]+", instruction)[:4] or ["company"]
            return ToolCall(cid, "search_docs", {"query": " ".join(words)})
        if "read_doc" in tools and "read_doc" not in used and last:
            m = re.search(r"(doc-\d+)", last[-1].content)
            if m:
                return ToolCall(cid, "read_doc", {"doc_id": m.group(1)})
        if "wiki_read" in tools and "wiki_read" not in used:
            wiki = self._enum(tools["wiki_read"], "properties", "wiki")[0]
            return ToolCall(cid, "wiki_read", {"wiki": wiki})
        if "run_tests" in tools and "run_tests" not in used:
            return ToolCall(cid, "run_tests", {})
        props = tools["submit"].parameters["properties"] if "submit" in tools else {}
        args = {"report": f"[mock] placeholder report from {me} ({len(used)} tool calls)"}
        if "answer" in props:
            args["answer"] = "[mock] unknown"
        return ToolCall(cid, "submit", args)
