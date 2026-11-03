"""Tools available to agents. Which tools an agent gets depends on its position (manager or
hands-on worker), its level settings, the task, and the org's communication policy."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .prompts import assignment_message
from .providers import ToolCall, ToolResult, ToolSpec
from .sandbox import run_python, truncate

if TYPE_CHECKING:
    from .agents import Agent
    from .runner import RunContext

READ_ONLY_FILES = {"SPEC.md"}
MAX_READ = 20000
MAX_WRITE = 200_000


class ToolError(Exception):
    pass


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


def tool_specs(agent: "Agent", ctx: "RunContext") -> list[ToolSpec]:
    task, org = ctx.task, ctx.org
    specs: list[ToolSpec] = []
    hands_on = not agent.children or agent.spec.can_do_work
    review = bool(agent.children) and agent.spec.review_tools

    if agent.children:
        names = [c.name for c in agent.children]
        specs.append(ToolSpec(
            "delegate",
            "Assign work to one or more of your direct reports. All assignments in one call run in parallel; "
            "the tool returns when every assignee has submitted their report. Each assignment must be "
            "self-contained: the assignee sees only the text you give them.",
            _obj({"assignments": {
                "type": "array",
                "items": _obj({"to": {"type": "string", "enum": names, "description": "name of the direct report"},
                               "task": {"type": "string", "description": "complete, self-contained instructions"}},
                              ["to", "task"]),
            }}, ["assignments"]),
        ))
    if task.has_workspace and (hands_on or review):
        specs.append(ToolSpec("list_files", "List files in the shared workspace.", _obj({}, [])))
        specs.append(ToolSpec("read_file", "Read a text file from the shared workspace.",
                              _obj({"path": {"type": "string"}}, ["path"])))
        if task.has_public_tests:
            specs.append(ToolSpec("run_tests", "Run the public example tests against the current workspace.",
                                  _obj({}, [])))
    if task.has_workspace and hands_on:
        specs.append(ToolSpec("write_file", "Create or overwrite a text file in the shared workspace.",
                              _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])))
        specs.append(ToolSpec("run_python",
                              "Execute a Python 3 snippet with the workspace as working directory (and on sys.path). "
                              "Returns stdout/stderr. 20 second limit.",
                              _obj({"code": {"type": "string"}}, ["code"])))
    if task.corpus is not None and hands_on:
        specs.append(ToolSpec("search_docs", "Keyword search over the knowledge base. Returns document ids, titles "
                              "and snippets.", _obj({"query": {"type": "string"}}, ["query"])))
        specs.append(ToolSpec("read_doc", "Read a knowledge-base document by id (e.g. doc-012) or exact title.",
                              _obj({"doc_id": {"type": "string"}}, ["doc_id"])))
    if org.communication != "tree":
        specs.append(ToolSpec("post_note", "Post a short note to your team's shared board.",
                              _obj({"text": {"type": "string"}}, ["text"])))
        specs.append(ToolSpec("read_notes", "Read your team's shared board.", _obj({}, [])))

    if agent.parent is None:
        props = {"answer": {"type": "string", "description": "the final answer only"},
                 "report": {"type": "string", "description": "short summary of how the work was done"}}
        required = ["answer"] if task.answer_required else ["report"]
        specs.append(ToolSpec("submit", "Deliver the final result to the client. This ends your work.",
                              _obj(props, required)))
    else:
        specs.append(ToolSpec("submit", f"Send your report to {agent.parent.name}. This ends your current assignment.",
                              _obj({"report": {"type": "string"}}, ["report"])))
    return specs


def validate_args(spec: ToolSpec, args: dict) -> str | None:
    props = spec.parameters.get("properties", {})
    for key in spec.parameters.get("required", []):
        if key not in args or args[key] in (None, ""):
            return f"missing required argument '{key}'"
    types = {"string": str, "array": list, "object": dict, "integer": int, "number": (int, float), "boolean": bool}
    for key, val in args.items():
        expected = props.get(key, {}).get("type")
        if expected and not isinstance(val, types[expected]):
            if expected == "string" and isinstance(val, (int, float)):
                continue  # tolerate numeric answers
            return f"argument '{key}' should be of type {expected}"
    return None


class Toolbox:
    def __init__(self, ctx: "RunContext"):
        self.ctx = ctx
        self.ws = ctx.workspace.resolve()

    async def execute(self, agent: "Agent", call: ToolCall, specs: dict[str, ToolSpec]) -> ToolResult:
        if call.error:
            return ToolResult(call.id, f"Tool call rejected: {call.error}", True)
        spec = specs.get(call.name)
        if spec is None:
            return ToolResult(call.id, f"Unknown tool '{call.name}'. Available: {', '.join(specs)}", True)
        problem = validate_args(spec, call.arguments)
        if problem:
            return ToolResult(call.id, f"Invalid arguments for {call.name}: {problem}", True)
        try:
            handler = getattr(self, f"_t_{call.name}")
            out = await handler(agent, call.arguments)
            self.ctx.event(agent, "tool", {"name": call.name, "ok": True})
            return ToolResult(call.id, out)
        except ToolError as e:
            self.ctx.event(agent, "tool", {"name": call.name, "ok": False, "error": str(e)[:200]})
            return ToolResult(call.id, f"Error: {e}", True)

    # ---- delegation ----
    async def _t_delegate(self, agent: "Agent", args: dict) -> str:
        ctx = self.ctx
        by_name = {c.name: c for c in agent.children}
        jobs: dict[str, str] = {}
        problems = []
        for a in args.get("assignments") or []:
            if not isinstance(a, dict):
                problems.append("each assignment must be an object with 'to' and 'task'")
                continue
            to, text = a.get("to"), (a.get("task") or "").strip()
            if to not in by_name:
                problems.append(f"'{to}' is not one of your direct reports ({', '.join(by_name)})")
            elif not text:
                problems.append(f"empty task for {to}")
            else:
                jobs[to] = f"{jobs[to]}\n\n{text}" if to in jobs else text
        if not jobs:
            raise ToolError("no valid assignments. " + "; ".join(problems))

        ctx.delegate_rounds[agent.name] += 1
        terms = ctx.task.key_terms
        for to, text in jobs.items():
            child = by_name[to]
            ctx.assign_counts[to] += 1
            low = text.lower()
            ctx.event(agent, "assign", {
                "to": to, "to_level": child.level, "leaf": not child.children, "chars": len(text),
                "round": ctx.assign_counts[to], "terms": [t for t in terms if t.lower() in low],
            })

        async with asyncio.TaskGroup() as tg:
            futures = {to: tg.create_task(by_name[to].handle(assignment_message(agent, text), ctx))
                       for to, text in jobs.items()}

        parts = [f"Note: {p}" for p in problems]
        for to, fut in futures.items():
            rep = fut.result()
            ctx.event(by_name[to], "report", {"to": agent.name, "chars": len(rep["report"]), "status": rep["status"]})
            parts.append(f"### Report from {to} [{rep['status']}]\n{rep['report']}")
        if ctx.task.has_workspace:
            score = await asyncio.to_thread(ctx.task.progress, ctx.workspace)
            if score is not None:
                ctx.event(agent, "progress", {"score": score, "round": ctx.delegate_rounds[agent.name]})
        return "\n\n".join(parts)

    # ---- workspace ----
    def _path(self, rel: str) -> Path:
        p = (self.ws / rel.strip().lstrip("/")).resolve()
        if not p.is_relative_to(self.ws):
            raise ToolError("path is outside the workspace")
        return p

    async def _t_list_files(self, agent, args) -> str:
        files = []
        for root, dirs, names in os.walk(self.ws):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
            for n in sorted(names):
                p = Path(root) / n
                files.append(f"{p.relative_to(self.ws)} ({p.stat().st_size} bytes)")
        return "\n".join(files[:300]) or "(workspace is empty)"

    async def _t_read_file(self, agent, args) -> str:
        p = self._path(args["path"])
        if not p.is_file():
            raise ToolError(f"no such file: {args['path']}")
        return truncate(p.read_text(errors="replace"), MAX_READ)

    async def _t_write_file(self, agent, args) -> str:
        rel, content = args["path"], args["content"]
        p = self._path(rel)
        if str(p.relative_to(self.ws)) in READ_ONLY_FILES:
            raise ToolError(f"{rel} is read-only")
        if len(content) > MAX_WRITE:
            raise ToolError("file too large")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self.ctx.event(agent, "write", {"path": str(p.relative_to(self.ws)), "chars": len(content)})
        return f"Wrote {len(content)} chars to {p.relative_to(self.ws)}"

    async def _t_run_python(self, agent, args) -> str:
        return await run_python(args["code"], self.ws)

    async def _t_run_tests(self, agent, args) -> str:
        return truncate(await asyncio.to_thread(self.ctx.task.public_tests, self.ws))

    # ---- knowledge base ----
    async def _t_search_docs(self, agent, args) -> str:
        hits = self.ctx.task.corpus.search(args["query"], k=6)
        if not hits:
            return "No matching documents."
        return "\n".join(f"{d.doc_id} | {d.title} | {d.text[:110]}..." for d, _ in hits)

    async def _t_read_doc(self, agent, args) -> str:
        doc = self.ctx.task.corpus.get(str(args["doc_id"]))
        if doc is None:
            raise ToolError(f"no document {args['doc_id']!r}; use search_docs to find ids")
        return f"{doc.doc_id} | {doc.title}\n\n{doc.text}"

    # ---- boards ----
    async def _t_post_note(self, agent, args) -> str:
        note = f"[{agent.name}] {args['text'][:2000]}"
        for key in self.ctx.board_keys(agent):
            self.ctx.boards.setdefault(key, []).append(note)
        self.ctx.event(agent, "note", {"chars": len(note)})
        return "Posted."

    async def _t_read_notes(self, agent, args) -> str:
        notes = []
        for key in self.ctx.board_keys(agent):
            notes += [n for n in self.ctx.boards.get(key, []) if n not in notes]
        return truncate("\n".join(notes[-60:])) or "(board is empty)"
