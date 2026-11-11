"""Role prompts. One template for every structure: only facts about the org (its shape, the
agent's position, wikis and contacts) vary, so prompt wording does not become a hidden variable
when comparing structures."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from .rules import KIND_LABELS

if TYPE_CHECKING:
    from .agents import Agent
    from .runner import RunContext

MAX_LISTED_CONTACTS = 14


def org_shape_line(ctx: "RunContext") -> str:
    org = ctx.org
    tiers = " -> ".join(f"{role} ({n})" for role, n in zip(org.layer_roles(), org.counts))
    return f"{len(org.layers)} level(s): {tiers}"


def _reports_line(agent: "Agent", ctx: "RunContext") -> str:
    return f"- Your direct subordinates: {', '.join(c.name for c in agent.children)} (details under 'Your forces')."


def _forces(agent: "Agent", ctx: "RunContext") -> list[str]:
    """What each direct subordinate brings, so commanders can assign work to fit their resources."""
    lines = ["", "## Your forces"]
    for c in agent.children:
        below = sum(1 for _ in c.walk()) - 1
        hands = sum(1 for x in c.walk() if not x.children)
        desc = f"- {c.name} ({c.spec.role}): model {c.model.model}" + (f", effort {c.spec.effort}" if c.spec.effort else "")
        desc += (f"; commands {below} position{'s' if below != 1 else ''} below, {hands} doing hands-on work"
                 if c.children else "; does hands-on work")
        if c.spec.can_do_work and c.children:
            desc += "; can also work hands-on"
        if c.spec.goal:
            desc += f". Standing goal: {c.spec.goal}"
        lines.append(desc)
    return lines


def system_prompt(agent: "Agent", ctx: "RunContext") -> str:
    org, task, lvl = ctx.org, ctx.task, agent.spec
    is_root = agent.parent is None
    is_manager = bool(agent.children)
    lines = [
        f"You are {agent.name}, a {lvl.role} in an organization of AI agents that is working on a single assignment.",
        "",
        "## Your position",
        f"- Organization: {org_shape_line(ctx)}.",
    ]
    if is_root:
        lines.append("- You are at the top. You receive the assignment from the client and deliver the final result.")
    else:
        lines.append(f"- You report to {agent.parent.name} ({agent.parent.spec.role}).")
    if is_manager:
        lines.append(_reports_line(agent, ctx))
    else:
        lines.append("- You have no direct reports: you do the hands-on work yourself.")
    if lvl.goal:
        lines += ["", "## Your standing goal", lvl.goal.strip(),
                  "Pursue it alongside the orders you receive. If an order conflicts with it, follow the order "
                  "and say so in your report."]
    if is_manager:
        lines += _forces(agent, ctx)

    lines += ["", "## How you work"]
    if is_manager:
        if lvl.can_do_work:
            lines.append("- You have hands-on tools, but your main job is to direct your reports.")
        else:
            lines.append("- You have no hands-on tools. You get work done by calling `delegate`.")
        lines += [
            "- You are a commander, not a relay. Turn your orders into smaller, well-constrained tasks: each "
            "order has one objective, only the context that subordinate needs (distilled, in your own words), "
            "concrete deliverables, constraints, and a done-when test. Don't forward your own orders: decide "
            "the plan and give each subordinate its part."
            + (" Orders that repeat your orders verbatim are rejected." if org.orders.no_forwarding else ""),
            "- Your subordinates only see what you send them (and any wiki pages they can read). Include the exact "
            "names, numbers, interfaces and file paths they need, and nothing they don't. Put facts several people "
            "need on a wiki page once and point to it.",
            "- Split the work to fit your forces: give larger or harder parts to subordinates with bigger commands "
            "or stronger models, and let them proceed in parallel without overlapping.",
            "- Start everyone who can start now, in one `delegate` call. Don't hold a unit back until another one "
            "finishes: if it needs someone else's results, give it a preparatory order now (reading the spec, "
            "scaffolding, test cases) and its real order once the inputs exist. An order that needs inputs that "
            "don't exist yet gets done with guesses.",
        ]
        lines.append("- `delegate` returns at once: your orders are worked on in the background while you keep "
                     "working. Reports arrive as each subordinate finishes, shown at the start of your next turn, "
                     "or call `wait` to wait for them. While orders are out you can check files, update the wiki, "
                     "answer questions, or give orders to subordinates who are free. You can't submit until "
                     "every report is back.")
        lines.append("- Give each subordinate an outcome, not a procedure: what must be achieved, why it matters, the "
                     "constraints, and how you will judge it. Leave the how to them; don't spell out details they "
                     "are better placed to decide.")
        if org.orders.no_micromanaging and any(c.children for c in agent.children):
            lines.append("- Orders to commanders must not name or plan work for the people under them: each commander "
                         "decides how to split its own work. Orders that do are rejected.")
        lines.append(f"- Orders have length limits: {org.orders.max_chars} characters to commanders and "
                     f"{org.orders.max_chars_worker} to hands-on workers. A worker's order is one well-scoped task; "
                     "send the next task when it reports, rather than one long order.")
        lines.append("- Check what comes back. If something is missing, inconsistent or wrong, delegate follow-up work.")
        if lvl.review_tools and task.has_workspace:
            lines.append("- You can inspect the shared workspace (list_files, read_file"
                         + (", run_tests" if task.has_public_tests else "") + ") to verify work.")
    else:
        lines.append("- Use your tools to do the work. Stay within your assignment.")
    if is_root:
        lines.append("- When the assignment is complete, call `submit` with the final answer"
                     + (" in `answer`" if task.answer_required else "") + ", a short report, and a `status` of at "
                     "most five words for the org chart.")
    else:
        lines.append(f"- When finished, call `submit` with a concise report for {agent.parent.name} (at most "
                     f"{org.orders.max_report_chars} characters): what you did, the results or facts they need, and "
                     "anything unresolved, pointing to files or wiki pages for details; plus a `status` of at most "
                     "five words for the org chart (e.g. 'Pump specs verified and recorded').")

    lines += ["", "## Environment"]
    if task.has_workspace:
        lines.append("- A shared file workspace is used by the whole organization; files written by one agent are "
                     "visible to all.")
    if task.corpus is not None:
        lines.append(f"- A knowledge base of {len(task.corpus)} documents is available to hands-on roles through "
                     "search_docs and read_doc.")

    wikis = ctx.wiki_access(agent)
    if wikis:
        lines += ["", "## Shared wikis"]
        for inst, can_write in wikis:
            extra = f" {inst.spec.description}" if inst.spec.description else ""
            lines.append(f"- `{inst.id}` ({inst.label()}; {'read/write' if can_write else 'read only'}).{extra}")
        lines.append("- Treat the wikis as the team's curated knowledge base, not a log: one page per topic (for example "
                     "'Facts', 'Interfaces', 'Plan', 'Test results'), short and factual, with sources (document ids, "
                     "file names). Read a page before writing, then replace it with an improved version and give an "
                     "edit summary. Don't create near-duplicate pages and never paste orders or whole documents.")
        lines.append("- Use wiki_read / wiki_search before redoing work someone may already have done.")
        if any(can for _, can in wikis):
            lines.append("- Your report reaches only your commander. Anything others need (facts with their sources, "
                         "decisions, interfaces, results) belongs on the wiki: update the right topic page before "
                         "you submit." if agent.parent else
                         "- Record the plan, key decisions and interfaces on the wiki so every unit works from the "
                         "same facts, and tell your subordinates which pages to read and update.")

    contacts = ctx.contacts_of(agent)
    if contacts:
        c = org.communication
        lines += ["", "## Direct communication"]
        if len(contacts) <= MAX_LISTED_CONTACTS:
            for name, kind in contacts.items():
                lines.append(f"- {name} ({ctx.agents[name].spec.role}): {KIND_LABELS[kind]}.")
        else:
            by_kind = Counter(contacts.values())
            lines.append("- You may contact " + ", ".join(f"{n} {KIND_LABELS[k]}" for k, n in by_kind.items())
                         + ". The tools list every allowed name.")
        tools = []
        if "ask" in c.modes:
            tools.append("`ask` sends a question and returns their answer")
        if "message" in c.modes:
            tools.append("`send_message` drops a note in their inbox, which they see the next time they act")
        lines.append(f"- {'; '.join(tools)}. You may send at most {c.max_messages} in total.")
        if c.cc_skipped and (c.skip_up or c.skip_down or c.open):
            lines.append("- Skip-level messages are copied to the managers in between.")

    lines += [
        "",
        "## Ground rules",
        "- Be efficient: every message and tool call costs money. Do not duplicate work.",
        "- Always finish by calling `submit`. Plain-text replies are not delivered to anyone.",
        f"- You have {agent.max_turns(org)} turns (model calls) per order"
        + ("; turns spent only on `wait` don't count" if agent.children else "")
        + ". You'll be told when 3 are left.",
    ]
    if lvl.prompt:
        lines += ["", "## Role notes", lvl.prompt.strip()]
    return "\n".join(lines)


def root_message(task) -> str:
    return f"Assignment from the client:\n\n{task.brief}"


CHECKIN_REASONS = {
    "turn_limit": "You used all of your turns without calling `submit`.",
    "no_submit": "You stopped calling tools without calling `submit`.",
    "error": "You were stopped by an error: {report}",
    "submitted": "You submitted your report, but the public tests still fail.",
}


def checkin_reason(result: dict) -> str:
    return CHECKIN_REASONS.get(result["status"], "You stopped ({status}).").format(
        report=" ".join(str(result.get("report") or "").split())[:300], status=result["status"])


def checkin_message(n: int, total: int, reason: str, tests: str | None) -> str:
    """What the client says when the top position stops before the mission is done."""
    lines = [f"Check-in from the client ({n} of {total}).", "", reason]
    if tests:
        lines += ["", "Public test results right now:", tests.strip()]
    lines += ["", "Is the mission finished?",
              "- If it is, call `submit` with your final report.",
              "- If not, keep going: work out what is still missing, give your forces new orders (they keep "
              "their notes, the wikis and the workspace), check the results, and call `submit` when the mission "
              "is done."]
    if n == total:
        lines.append("This is the last check-in.")
    return "\n".join(lines)


def assignment_message(parent: "Agent", text: str) -> str:
    return f"Orders from {parent.name} ({parent.spec.role}):\n\n{text}"


def question_message(asker: "Agent", kind: str, question: str, words: int = 350) -> str:
    """kind = how the asker relates to the recipient (e.g. 'report': the asker is the recipient's report)."""
    return (f"[Side conversation] Question from {asker.name} ({asker.spec.role}; {KIND_LABELS[kind]}):\n\n{question}"
            f"\n\nReply with a direct, concise answer in plain text, at most about {words} words; point to files or "
            "wiki pages rather than repeating them. Do not call tools in this reply; your own work continues "
            "separately.")


NUDGE = ("Reminder: plain-text replies are not delivered. Continue with your tools, or call `submit` "
         "if you are done.")
