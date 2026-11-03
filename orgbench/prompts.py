"""Role prompts. One template for every structure: only org-shape facts vary, so prompt
wording does not become a hidden variable when comparing structures."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agents import Agent
    from .config import OrgConfig
    from .tasks import TaskInstance


def org_shape_line(org: "OrgConfig") -> str:
    tiers = " -> ".join(f"{lvl.role} ({n})" for lvl, n in zip(org.levels, org.counts))
    return f"{len(org.levels)} level(s): {tiers}"


def system_prompt(agent: "Agent", org: "OrgConfig", task: "TaskInstance") -> str:
    lvl = agent.spec
    is_root = agent.parent is None
    is_manager = bool(agent.children)
    lines = [
        f"You are {agent.name}, a {lvl.role} in an organization of AI agents that is working on a single assignment.",
        "",
        "## Your position",
        f"- Organization: {org_shape_line(org)}.",
    ]
    if is_root:
        lines.append("- You are at the top. You receive the assignment from the client and deliver the final result.")
    else:
        lines.append(f"- You report to {agent.parent.name} ({agent.parent.spec.role}).")
    if is_manager:
        child_role = agent.children[0].spec.role
        names = ", ".join(c.name for c in agent.children)
        extra = ""
        if agent.children[0].children:
            sub = agent.children[0].children[0].spec.role
            extra = f" Each of them manages their own team of {sub}s."
        else:
            extra = " They do the hands-on work."
        if org.disclose_models:
            extra += f" They run on model '{agent.children[0].spec.model}'."
        lines.append(f"- Your direct reports ({child_role}): {names}.{extra}")
    else:
        lines.append("- You have no direct reports: you do the hands-on work yourself.")

    lines += ["", "## How you work"]
    if is_manager:
        if lvl.can_do_work:
            lines.append("- You have hands-on tools, but your main job is to direct your reports.")
        else:
            lines.append("- You have no hands-on tools. You get work done by calling `delegate`.")
        lines += [
            "- Your reports only see what you send them. Make each assignment self-contained: include every detail "
            "they need (exact names, specifications, formats, file paths, what to report back).",
            "- Split the work so reports can proceed in parallel without overlapping. Put several assignments in one "
            "`delegate` call; they run at the same time and you get all reports back together.",
            "- Check what comes back. If something is missing, inconsistent or wrong, delegate follow-up work.",
        ]
        if lvl.review_tools and task.has_workspace:
            lines.append("- You can inspect the shared workspace (list_files, read_file"
                         + (", run_tests" if task.has_public_tests else "") + ") to verify work.")
    else:
        lines += [
            "- Use your tools to do the work. Stay within your assignment.",
        ]
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
    if org.communication == "team_board":
        lines.append("- You share a notes board with your immediate team (your manager and fellow reports, or you "
                     "and your own reports): post_note / read_notes.")
    elif org.communication == "global_board":
        lines.append("- Everyone in the organization shares one notes board: post_note / read_notes.")

    lines += [
        "",
        "## Ground rules",
        "- Be efficient: every message and tool call costs money. Do not duplicate work.",
        "- Always finish by calling `submit`. Plain-text replies are not delivered to anyone.",
    ]
    if lvl.prompt:
        lines += ["", "## Role notes", lvl.prompt.strip()]
    return "\n".join(lines)


def root_message(task: "TaskInstance") -> str:
    return f"Assignment from the client:\n\n{task.brief}"


def assignment_message(parent: "Agent", text: str) -> str:
    return f"Assignment from {parent.name} ({parent.spec.role}):\n\n{text}"


NUDGE = ("Reminder: plain-text replies are not delivered. Continue with your tools, or call `submit` "
         "if you are done.")
