"""Turn budgets agents know about, last-turn submits, messages that wake `wait`, and small wiki repairs."""

import asyncio

from swarmy.providers import StepResult, ToolCall, Usage
from swarmy.providers.mock_provider import MockProvider

from .test_order_rules import _call, _ctx

SUBMIT = ("submit", {"report": "Pump flow is 10 l/min.", "status": "Pump flow found"})


def _scripted(tmp_path, who, script, **org_extra):
    """`who` follows `script` (others use the mock policy). Returns (result, user/tool texts it saw, ctx)."""
    seen, steps = [], iter(script)

    class Scripted(MockProvider):
        async def step(self, conv, spec, *, max_tokens, effort, tool_choice=None):
            if not conv.system.startswith(f"You are {who},"):
                return await super().step(conv, spec, max_tokens=max_tokens, effort=effort, tool_choice=tool_choice)
            for kind, payload in conv.events[len(seen_events):]:
                if kind == "user":
                    seen.append(payload)
                elif kind == "tool":
                    seen.extend(r.content for r in payload)
            seen_events[:] = conv.events
            name, args = next(steps)
            call = ToolCall(f"c{len(seen)}", name, args)
            conv.events.append(("assistant", [call]))
            seen_events.append(("assistant", [call]))
            return StepResult(text="", tool_calls=[call], usage=Usage(), stop_reason="tool_use", latency_s=0)

    seen_events: list = []
    ctx, root = _ctx(tmp_path, **org_extra)
    ctx.registry._providers["mock"] = Scripted()
    agent = ctx.agents[who]
    text = "Assignment from the client: go." if agent.parent is None else "Orders from captain: find the pump flow."
    rep = asyncio.run(agent.handle(text, ctx, order_id=0 if agent.parent is None else 1))
    return rep, seen, ctx


def test_a_submit_on_the_last_turn_is_never_blocked_by_the_wiki_reminder(tmp_path):
    rep, seen, ctx = _scripted(tmp_path, "private-1", [SUBMIT], worker_max_turns=1)
    assert rep["status"] == "submitted" and rep["status_line"] == "Pump flow found"
    assert not ctx.tracer.query("SELECT 1 FROM events WHERE kind='wiki_reminder'")
    assert any("This is your last turn" in s for s in seen)


def test_agents_are_warned_before_they_run_out_of_turns(tmp_path):
    rep, seen, ctx = _scripted(tmp_path, "private-1", [("list_files", {})] * 5 + [SUBMIT], worker_max_turns=6,
                               knowledge={"wikis": []})
    assert rep["status"] == "submitted"
    assert any(s.startswith("You have 3 turns left for this order") for s in seen)
    assert any(s.startswith("This is your last turn") for s in seen)
    assert "You have 6 turns (model calls) per order" in ctx.agents["private-1"].conv.system


def test_turns_spent_waiting_do_not_count(tmp_path):
    order = {"to": "private-1", "objective": "Find the pump flow.", "deliverables": "The number.", "done_when": "Quoted."}
    rep, seen, _ = _scripted(tmp_path, "captain", [
        ("list_files", {}), ("delegate", {"assignments": [order]}), ("wait", {"for": "all"}), SUBMIT,
    ], manager_max_turns=3, knowledge={"wikis": []})
    assert rep["status"] == "submitted"  # 4 calls on a budget of 3: the wait was free


def test_a_message_wakes_a_commander_that_is_waiting(tmp_path):
    ctx, root = _ctx(tmp_path)

    async def go():
        stuck = asyncio.ensure_future(asyncio.get_running_loop().create_future())
        root.outstanding[99] = {"to": "private-1", "task": stuck}
        waiter = asyncio.create_task(ctx.tools._t_wait(root, {"for": "all"}))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        ctx.tools._post(root.name, "[Message from private-2 (private)]: the facts page is up")
        out = await asyncio.wait_for(waiter, 1)
        stuck.cancel()
        return out
    out = asyncio.run(go())
    assert "the facts page is up" in out and "Still out: #99 (private-1)" in out


def test_side_conversation_notes_do_not_wake_wait(tmp_path):
    ctx, root = _ctx(tmp_path)
    ctx.tools._post(root.name, "[FYI] you answered a question", wake=False)
    assert not root.message_waiting


def test_no_wiki_reminder_when_results_are_already_in_files(tmp_path):
    rep, seen, ctx = _scripted(tmp_path, "private-1", [
        ("write_file", {"path": "notes/pump.md", "content": "Pump flow: 10 l/min (doc-004)"}), SUBMIT])
    assert rep["status"] == "submitted"
    assert not ctx.tracer.query("SELECT 1 FROM events WHERE kind='wiki_reminder'")


def test_wiki_page_title_from_heading_and_tolerant_lookup(tmp_path):
    ctx, root = _call_ctx = _ctx(tmp_path)
    r = _call(ctx, root, "wiki_write", {"wiki": "orders", "content": "# Pump facts\n\nFlow is 10 l/min (doc-004)."})
    assert not r.is_error and ctx.wiki.find("orders", "Pump facts") is not None
    listing = _call(ctx, root, "wiki_read", {"wiki": "orders"}).content
    assert '- "Pump facts": v1, last edited by captain' in listing
    assert not _call(ctx, root, "wiki_read", {"wiki": "orders", "page": "Pump facts (v1"}).is_error
    assert not _call(ctx, root, "wiki_read", {"wiki": "orders", "page": '"Pump facts"'}).is_error
