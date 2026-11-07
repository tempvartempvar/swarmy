"""Context trimming, communication and report limits, scoped orders and no micromanagement."""

import asyncio
import copy
import json

from anthropic.types import TextBlock, ThinkingBlock, ToolUseBlock

from swarmy.agents import build_tree
from swarmy.budget import RunBudget
from swarmy.catalog import Catalog
from swarmy.config import OrgConfig
from swarmy.prompts import question_message, system_prompt
from swarmy.providers import ProviderRegistry, StepResult, ToolCall, ToolResult, Usage
from swarmy.providers.anthropic_provider import AnthropicConversation
from swarmy.providers.base import TRIM_MARK
from swarmy.providers.mock_provider import MockConversation, MockProvider
from swarmy.providers.openai_provider import OpenAIConversation
from swarmy.runner import RunContext
from swarmy.tasks import make_task
from swarmy.tools import Toolbox
from swarmy.tracing import Tracer, Transcript

from .test_order_rules import _call, _ctx

ORDER = "Orders from captain: implement sim.py. " + "detail " * 200


def _openai_conv(turns=6):
    conv = OpenAIConversation("system prompt", [])
    conv.add_user(ORDER)
    for i in range(turns):
        conv.add_assistant({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "write_file", "arguments": json.dumps({"path": "a.py", "content": "x" * 5000})}}]})
        conv.add_tool_results([ToolResult(f"c{i}", "y" * 5000)])
    return conv


def test_openai_trim_clips_old_turns_and_keeps_the_order_and_recent_turns():
    conv = _openai_conv()
    before = conv.approx_input_tokens()
    saved = conv.trim(keep_turns=2, keep_chars=200)
    assert saved > 0 and conv.approx_input_tokens() < before / 2
    msgs = conv.messages
    assert msgs[1]["content"] == ORDER  # the order is intact
    old_tool, old_call = msgs[3], json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"])
    assert TRIM_MARK in old_tool["content"] and TRIM_MARK in old_call["content"] and old_call["path"] == "a.py"
    assert msgs[-1]["content"] == "y" * 5000 and msgs[-3]["content"] == "y" * 5000  # last two turns intact
    snapshot = copy.deepcopy(msgs)
    assert conv.trim(keep_turns=2, keep_chars=200) == 0 and conv.messages == snapshot  # idempotent: cache-friendly


def test_anthropic_trim_leaves_thinking_blocks_alone():
    conv = AnthropicConversation("system prompt", [])
    conv.add_user(ORDER)
    thinking = ThinkingBlock(type="thinking", thinking="t" * 3000, signature="sig")
    for i in range(3):
        conv.add_assistant([thinking, TextBlock(type="text", text="note " * 400),
                            ToolUseBlock(type="tool_use", id=f"t{i}", name="write_file",
                                         input={"path": "a.py", "content": "x" * 5000})])
        conv.add_tool_results([ToolResult(f"t{i}", "y" * 5000)])
    assert conv.trim(keep_turns=1, keep_chars=200) > 0
    first_assistant = conv.messages[1]["content"]
    assert first_assistant[0] is thinking
    assert TRIM_MARK in first_assistant[1].text and TRIM_MARK in first_assistant[2].input["content"]
    assert TRIM_MARK in conv.messages[2]["content"][0]["content"]
    assert conv.messages[0]["content"][0]["text"] == ORDER
    assert conv.messages[-1]["content"][0]["content"] == "y" * 5000


def test_mock_trim_keeps_the_instruction():
    conv = MockConversation("You are private-1, a private.", [])
    conv.add_user(ORDER)
    for i in range(4):
        conv.events.append(("assistant", [ToolCall(f"m{i}", "read_doc", {})]))
        conv.add_tool_results([ToolResult(f"m{i}", "z" * 4000)])
    assert conv.trim(1, 200) > 0 and conv.episode()[0] == ORDER
    assert TRIM_MARK in conv.events[2][1][0].content and TRIM_MARK not in conv.events[-1][1][0].content


def test_agents_trim_their_context_when_over_budget(tmp_path):
    from .test_checkins import _run
    org = OrgConfig.model_validate({"name": "t", "levels": [{"role": "captain", "model": "mock", "span": 2},
                                                            {"role": "private", "model": "mock"}],
                                    "context": {"max_tokens": 2000, "keep_turns": 1, "trim_to": 100}})
    s, events, log, _ = _run(org, tmp_path, task=("greenhouse", {}))
    trims = [d for _, k, d in events if k == "trimmed"]
    assert s["status"] == "completed" and trims and all(d["after"] < d["before"] for d in trims)
    assert "context trimmed from" in log


def test_messages_and_questions_have_length_limits(tmp_path):
    ctx, root = _ctx(tmp_path, communication={"chain": True, "max_message_chars": 200, "max_question_chars": 150})
    r = _call(ctx, root, "send_message", {"to": "private-1", "text": "x" * 300})
    assert r.is_error and "message is 300 characters; keep it under 200" in r.content
    assert ctx.messages_sent[root.name] == 0  # a rejected message doesn't use up the allowance
    assert not _call(ctx, root, "send_message", {"to": "private-1", "text": "Pump specs are on the wiki."}).is_error
    r = _call(ctx, root, "ask", {"to": "private-1", "question": "q" * 200})
    assert r.is_error and "question is 200 characters; keep it under 150" in r.content
    assert "at most about 360 words" in question_message(root, "report", "Where is the pump spec?", words=360)


def test_long_reports_are_rejected_once_then_cut(tmp_path):
    reports = iter(["r" * 5000, "s" * 5000])

    class Reporter(MockProvider):
        async def step(self, conv, spec, *, max_tokens, effort, tool_choice=None):
            call = ToolCall("s", "submit", {"report": next(reports), "status": "Done"})
            conv.events.append(("assistant", [call]))
            return StepResult(text="", tool_calls=[call], usage=Usage(), stop_reason="tool_use", latency_s=0)

    ctx, root = _ctx(tmp_path, knowledge={"wikis": []})
    ctx.registry._providers["mock"] = Reporter()
    rep = asyncio.run(ctx.agents["private-1"].handle("Orders from captain: go.", ctx, order_id=1))
    assert rep["status"] == "submitted" and rep["report"].startswith("s" * 3000)
    assert "characters cut: over the report limit" in rep["report"] and len(rep["report"]) < 3200


def test_orders_to_workers_are_shorter(tmp_path):
    ctx, root = _ctx(tmp_path)
    long = {"to": "private-1", "objective": "Implement sim.py.", "context": "c" * 1900, "deliverables": "sim.py",
            "done_when": "run_tests passes."}
    r = _call(ctx, root, "delegate", {"assignments": [long]})
    assert r.is_error and "orders to hands-on workers must stay under 1800" in r.content
    assert not _call(ctx, root, "delegate", {"assignments": [{**long, "context": "c" * 900}]}).is_error


def _three_levels(tmp_path):
    org = OrgConfig.model_validate({"name": "m", "levels": [
        {"role": "field-marshal", "model": "mock", "span": 1}, {"role": "captain", "model": "mock", "span": 2},
        {"role": "private", "model": "mock"}], "knowledge": {"wikis": []}})
    root = build_tree(org, Catalog.load(offline=True))
    task = make_task("greenhouse", {}, 1)
    task.setup_workspace(tmp_path / "ws")
    ctx = RunContext(run_id="r", org=org, task=task, workspace=tmp_path / "ws", registry=ProviderRegistry(mock=True),
                     budget=RunBudget(1), tracer=Tracer(tmp_path / "db"), transcript=Transcript(tmp_path / "t"))
    ctx.tools = Toolbox(ctx)
    ctx.bind(root)
    root.current_order_text = "Assignment from the client: build it."
    return ctx, root


def test_orders_to_commanders_cannot_assign_work_below_them(tmp_path):
    ctx, root = _three_levels(tmp_path)
    captain = root.children[0]
    private = captain.children[0].name
    order = {"to": captain.name, "objective": "Deliver a simulator that passes the sample cases.",
             "deliverables": "sim.py and a short report.", "done_when": "run_tests shows 4/4 sim cases."}
    r = _call(ctx, root, "delegate", {"assignments": [{**order, "constraints": f"Have {private} write the code."}]})
    assert r.is_error and f"assigns work below {captain.name} (it names {private})" in r.content
    assert not _call(ctx, root, "delegate", {"assignments": [order]}).is_error
    assert "must not name or plan work for the people under them" in system_prompt(root, ctx)
    assert "must not name" not in system_prompt(captain, ctx)  # its subordinates have nobody under them
