"""Non-blocking delegation: commanders keep working while orders are out, and collect reports with `wait`."""

import asyncio
import json

from swarmy.providers import StepResult, ToolCall, Usage
from swarmy.providers.mock_provider import MockProvider
from swarmy.tools import tool_specs

from .test_order_rules import _call, _ctx


def _order(to, objective="Find the pump flow.", **extra):
    return {"to": to, "objective": objective, "deliverables": "The number, with its doc id.",
            "done_when": "You quote the doc.", **extra}


def _captain_script(tmp_path, script, org_extra=None, pause=None):
    """The captain follows `script` (a list of (tool, args) or callables); privates use the mock policy.
    Returns (captain's result, tool results the captain saw, events, ctx)."""
    seen, steps = [], iter(script)

    class Scripted(MockProvider):
        async def step(self, conv, spec, *, max_tokens, effort, tool_choice=None):
            if not conv.system.startswith("You are captain"):
                return await super().step(conv, spec, max_tokens=max_tokens, effort=effort, tool_choice=tool_choice)
            if conv.events and conv.events[-1][0] == "tool":
                seen.append(conv.events[-1][1][0].content)
            elif conv.events and conv.events[-1][0] == "user":
                seen.append(conv.events[-1][1])
            if pause and len(seen) == pause:
                await asyncio.sleep(0.4)  # let the privates finish while the captain is busy
            name, args = next(steps)
            call = ToolCall(f"c{len(seen)}", name, args)
            conv.events.append(("assistant", [call]))
            return StepResult(text="", tool_calls=[call], usage=Usage(), stop_reason="tool_use", latency_s=0)

    ctx, root = _ctx(tmp_path, knowledge={"wikis": []}, **(org_extra or {}))
    ctx.registry._providers["mock"] = Scripted()
    rep = asyncio.run(root.handle("Assignment from the client: find the pump flow.", ctx, order_id=0))
    events = [(r["node"], r["kind"], json.loads(r["data_json"]))
              for r in ctx.tracer.query("SELECT node, kind, data_json FROM events ORDER BY id")]
    return rep, seen, events, ctx


SUBMIT = ("submit", {"report": "Pump flow is 10 l/min.", "status": "Pump flow found"})


def test_delegate_returns_at_once_and_wait_collects_reports(tmp_path):
    rep, seen, events, _ = _captain_script(tmp_path, [
        ("delegate", {"assignments": [_order("private-1"), _order("private-2", "Find the soil type.")]}),
        SUBMIT,  # too early: orders are still out
        ("wait", {"for": "all"}),
        SUBMIT,
    ])
    assert seen[1].startswith("Orders sent: #1 to private-1, #2 to private-2.")
    assert "orders are still out (#1 (private-1), #2 (private-2))" in seen[2] and "Call `wait`" in seen[2]
    assert "### Report from private-1 on order #1 [submitted]" in seen[3] and "All your orders have reported back" in seen[3]
    assert rep["status"] == "submitted"
    waits = [d for _, k, d in events if k == "wait"]
    assert waits and sorted(waits[0]["got"]) == ["private-1", "private-2"] and waits[0]["still_out"] == []
    kinds = [k for n, k, _ in events if n == "captain"]
    assert kinds.index("assign") < kinds.index("wait")


def test_reports_arrive_at_the_next_turn_without_waiting(tmp_path):
    rep, seen, events, _ = _captain_script(tmp_path, [
        ("delegate", {"assignments": [_order("private-1")]}),
        ("list_files", {}),  # keeps working; the private finishes meanwhile
        ("list_files", {}),
        SUBMIT,
    ], pause=2)
    delivered = [s for s in seen if s.startswith("Reports received:")]
    assert rep["status"] == "submitted" and not [k for _, k, _ in events if k == "wait"]
    assert delivered and "### Report from private-1 on order #1" in delivered[0]


def test_a_busy_subordinate_takes_one_order_at_a_time(tmp_path):
    rep, seen, _, _ = _captain_script(tmp_path, [
        ("delegate", {"assignments": [_order("private-1")]}),
        ("delegate", {"assignments": [_order("private-1", "Also find the soil type.")]}),
        ("wait", {}),
        SUBMIT,
    ])
    assert "private-1 is still working on order #1" in seen[2]


def test_orders_still_out_when_a_commander_stops_are_passed_up(tmp_path):
    rep, seen, _, _ = _captain_script(tmp_path, [
        ("delegate", {"assignments": [_order("private-1"), _order("private-2", "Find the soil type.")]}),
        ("list_files", {}),
    ], org_extra={"manager_max_turns": 2})
    assert rep["status"] == "turn_limit"
    assert "came back after I stopped" in rep["report"] and "### Report from private-2" in rep["report"]


def test_blocking_mode_returns_reports_from_delegate(tmp_path):
    rep, seen, events, ctx = _captain_script(tmp_path, [
        ("delegate", {"assignments": [_order("private-1"), _order("private-2", "Find the soil type.")]}),
        SUBMIT,
    ], org_extra={"orders": {"blocking": True}})
    assert "### Report from private-1" in seen[1] and "### Report from private-2" in seen[1]
    assert rep["status"] == "submitted"
    assert "wait" not in {s.name for s in tool_specs(ctx.agents["captain"], ctx)}


def test_order_fields_may_run_ten_percent_over(tmp_path):
    ctx, root = _ctx(tmp_path)
    ok = _call(ctx, root, "delegate", {"assignments": [_order("private-1", deliverables="d" * 1300)]})
    assert not ok.is_error and ok.content.startswith("Orders sent")
    bad = _call(ctx, root, "delegate", {"assignments": [_order("private-2", deliverables="d" * 1400)]})
    assert bad.is_error and "deliverables is 1400 characters; keep it under 1200" in bad.content


def test_hitting_the_output_limit_without_a_tool_gets_a_specific_reminder(tmp_path):
    seen = []

    class Rambler(MockProvider):
        async def step(self, conv, spec, *, max_tokens, effort, tool_choice=None):
            seen.append(conv.events[-1][1] if conv.events[-1][0] == "user" else None)
            if len(seen) == 1:
                return StepResult(text="", tool_calls=[], usage=Usage(output_tokens=max_tokens),
                                  stop_reason="max_tokens", latency_s=0)
            call = ToolCall("s", "submit", {"report": "done", "status": "Done"})
            conv.events.append(("assistant", [call]))
            return StepResult(text="", tool_calls=[call], usage=Usage(), stop_reason="tool_use", latency_s=0)

    ctx, root = _ctx(tmp_path, knowledge={"wikis": []})
    ctx.registry._providers["mock"] = Rambler()
    rep = asyncio.run(ctx.agents["private-1"].handle("Orders from captain: go.", ctx, order_id=1))
    assert rep["status"] == "submitted" and "hit the output limit" in seen[1] and "call one tool" in seen[1]
