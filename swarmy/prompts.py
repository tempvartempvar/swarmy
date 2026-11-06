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
    kids = agent.children
    roles = Counter(c.spec.role for c in kids)
    role_txt = ", ".join(f"{r}" if n == len(kids) else f"{n} {r}" for r, n in roles.items())
    line = f"- Your direct reports ({role_txt}): {', '.join(c.name for c in kids)}."
    managers = [c for c in kids if c.children]
    if managers and len(managers) == len(kids):
        line += " Each of them manages their own team."
    elif managers:
        line += f" Of these, {', '.join(c.name for c in managers)} manage their own teams; the rest do hands-on work."
    else:
        line += " They do the hands-on work."
    if ctx.org.disclose_models:
        line += " Models: " + ", ".join(f"{c.name}={c.spec.model}" for c in kids) + "."
    return line


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

    lines += ["", "## How you work"]
    if is_manager:
        if lvl.can_do_work:
            lines.append("- You have hands-on tools, but your main job is to direct your reports.")
        else:
            lines.append("- You have no hands-on tools. You get work done by calling `delegate`.")
        lines += [
            "- Your reports only see what you send them (and any wiki pages they can read). Make each assignment "
            "self-contained: include every detail they need (exact names, specifications, formats, file paths, what "
            "to report back).",
            "- Split the work so reports can proceed in parallel without overlapping. Put several assignments in one "
            "`delegate` call; they run at the same time and you get all reports back together.",
            "- Check what comes back. If something is missing, inconsistent or wrong, delegate follow-up work.",
        ]
        if lvl.review_tools and task.has_workspace:
            lines.append("- You can inspect the shared workspace (list_files, read_file"
                         + (", run_tests" if task.has_public_tests else "") + ") to verify work.")
    else:
        lines.append("- Use your tools to do the work. Stay within your assignment.")
    if is_root:
        lines.append("- When the assignment is complete, call `submit` with the final answer"
                     + (" in `answer`" if task.answer_required else "") + " and a short report.")
    else:
        lines.append(f"- When finished, call `submit` with a concise report for {agent.parent.name}: what you did, the "
                     "results or facts they need, and anything unresolved.")

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
        lines.append("- Use wiki_read / wiki_search to find what others have recorded, and wiki_write to record plans, "
                     "decisions, interfaces and findings that others will need. Check the wiki before redoing work.")

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
    ]
    if lvl.prompt:
        lines += ["", "## Role notes", lvl.prompt.strip()]
    return "\n".join(lines)


def root_message(task) -> str:
    return f"Assignment from the client:\n\n{task.brief}"


def assignment_message(parent: "Agent", text: str) -> str:
    return f"Assignment from {parent.name} ({parent.spec.role}):\n\n{text}"


def question_message(asker: "Agent", kind: str, question: str) -> str:
    """kind = how the asker relates to the recipient (e.g. 'report': the asker is the recipient's report)."""
    return (f"[Side conversation] Question from {asker.name} ({asker.spec.role}; {KIND_LABELS[kind]}):\n\n{question}"
            "\n\nReply with a direct, concise answer in plain text. Do not call tools in this reply; your own work "
            "continues separately.")


NUDGE = ("Reminder: plain-text replies are not delivered. Continue with your tools, or call `submit` "
         "if you are done.")
