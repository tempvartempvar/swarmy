"""Offline scripted provider. Exercises the whole pipeline (delegation, tools, logging,
grading, budgets) at zero cost. It does not try to solve tasks."""

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

    def episode(self) -> tuple[str, list[str], list[ToolResult]]:
        """Latest instruction, tools used since then, and the latest tool results."""
        instruction, used, last_results = "", [], []
        for kind, payload in self.events:
            if kind == "user":
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

    async def step(self, conv, spec: ModelSpec, *, max_tokens: int, effort: str | None) -> StepResult:
        assert isinstance(conv, MockConversation)
        await asyncio.sleep(0.002)
        instruction, used, last = conv.episode()
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

    def _policy(self, conv: MockConversation, instruction: str, used: list[str], last: list[ToolResult]) -> ToolCall:
        tools = conv.tools
        cid = f"mock_{next(_ids)}"
        if "delegate" in tools and "delegate" not in used:
            schema = tools["delegate"].parameters
            names = schema["properties"]["assignments"]["items"]["properties"]["to"].get("enum", [])
            assignments = [
                {"to": n, "task": f"[part {i + 1}/{len(names)}] {instruction[:600]}"} for i, n in enumerate(names)
            ]
            return ToolCall(cid, "delegate", {"assignments": assignments})
        if "search_docs" in tools and "search_docs" not in used:
            words = re.findall(r"[A-Z][a-z]+", instruction)[:4] or ["company"]
            return ToolCall(cid, "search_docs", {"query": " ".join(words)})
        if "read_doc" in tools and "read_doc" not in used and last:
            m = re.search(r"(doc-\d+)", last[-1].content)
            if m:
                return ToolCall(cid, "read_doc", {"doc_id": m.group(1)})
        if "run_tests" in tools and "run_tests" not in used:
            return ToolCall(cid, "run_tests", {})
        if "post_note" in tools and "post_note" not in used:
            return ToolCall(cid, "post_note", {"text": "mock note"})
        props = tools["submit"].parameters["properties"] if "submit" in tools else {}
        args = {"report": f"mock report ({len(used)} tool calls)"}
        if "answer" in props:
            args["answer"] = "unknown"
        return ToolCall(cid, "submit", args)
