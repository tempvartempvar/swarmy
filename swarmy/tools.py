"""Tools available to agents. Which tools an agent gets depends on its position (manager or
hands-on worker), its settings, the task, the wikis it can access and the org's communication
rules."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .prompts import assignment_message, question_message, system_prompt
from .providers import ProviderError, ToolCall, ToolResult, ToolSpec
from .rules import KIND_LABELS, relation, skipped_managers
from .sandbox import run_python, truncate

if TYPE_CHECKING:
    from .agents import Agent
    from .runner import RunContext

READ_ONLY_FILES = {"SPEC.md"}
MAX_READ = 20000
MAX_WRITE = 200_000
MAX_WIKI_PAGE = 30_000
ANSWER_TOKENS = 4000


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

    wikis = ctx.wiki_access(agent)
    if wikis:
        readable = [w.id for w, _ in wikis]
        writable = [w.id for w, can in wikis if can]
        specs.append(ToolSpec("wiki_read", "Read a shared wiki. Without `page`, lists its pages; with `page`, returns "
                              "that page.", _obj({"wiki": {"type": "string", "enum": readable},
                                                  "page": {"type": "string"}}, ["wiki"])))
        specs.append(ToolSpec("wiki_search", "Search the pages of a shared wiki by keywords.",
                              _obj({"wiki": {"type": "string", "enum": readable}, "query": {"type": "string"}},
                                   ["wiki", "query"])))
        if writable:
            specs.append(ToolSpec("wiki_write", "Create or update a page on a shared wiki. Set append=true to add to "
                                  "the end of an existing page instead of replacing it.",
                                  _obj({"wiki": {"type": "string", "enum": writable}, "page": {"type": "string"},
                                        "content": {"type": "string"}, "append": {"type": "boolean"}},
                                       ["wiki", "page", "content"])))

    contacts = ctx.contacts_of(agent)
    if contacts:
        names = list(contacts)
        modes = org.communication.modes
        if "ask" in modes:
            specs.append(ToolSpec("ask", "Ask an allowed colleague a question and wait for their answer.",
                                  _obj({"to": {"type": "string", "enum": names}, "question": {"type": "string"}},
                                       ["to", "question"])))
        if "message" in modes:
            specs.append(ToolSpec("send_message", "Leave a note in an allowed colleague's inbox (no reply). They see "
                                  "it the next time they act.",
                                  _obj({"to": {"type": "string", "enum": names}, "text": {"type": "string"}},
                                       ["to", "text"])))

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
        schema = props.get(key, {})
        expected = schema.get("type")
        if expected and not isinstance(val, types[expected]):
            if expected == "string" and isinstance(val, (int, float)):
                continue  # tolerate numeric answers
            return f"argument '{key}' should be of type {expected}"
        if "enum" in schema and val not in schema["enum"]:
            return f"'{val}' is not allowed for '{key}' (allowed: {', '.join(map(str, schema['enum']))})"
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
        order_ids: dict[str, int] = {}
        for to, text in jobs.items():
            child = by_name[to]
            ctx.assign_counts[to] += 1
            order_ids[to] = ctx.next_order()
            low = text.lower()
            ctx.event(agent, "assign", {
                "to": to, "to_level": child.level, "leaf": not child.children, "chars": len(text),
                "round": ctx.assign_counts[to], "terms": [t for t in terms if t.lower() in low],
                "order_id": order_ids[to], "parent_order": agent.current_order,
                "from_role": agent.spec.role, "to_role": child.spec.role, "text": text, "preview": text[:240],
            })

        ctx.live(agent, "waiting", {"on": list(jobs)})
        async with asyncio.TaskGroup() as tg:
            futures = {to: tg.create_task(by_name[to].handle(assignment_message(agent, text), ctx,
                                                             order_id=order_ids[to]))
                       for to, text in jobs.items()}
        ctx.live(agent, "working", {})

        parts = [f"Note: {p}" for p in problems]
        for to, fut in futures.items():
            rep = fut.result()
            ctx.event(by_name[to], "report", {"to": agent.name, "chars": len(rep["report"]), "status": rep["status"],
                                              "order_id": order_ids[to], "text": rep["report"],
                                              "preview": rep["report"][:240]})
            parts.append(f"### Report from {to} [{rep['status']}]\n{rep['report']}")
        if ctx.task.has_workspace:
            score = await asyncio.to_thread(ctx.task.progress, ctx.workspace)
            if score is not None:
                ctx.event(agent, "progress", {"score": score, "round": ctx.delegate_rounds[agent.name]})
        return "\n\n".join(parts)

    # ---- direct communication ----
    def _check_contact(self, agent: "Agent", to: str) -> tuple["Agent", str]:
        ctx = self.ctx
        kind = ctx.contacts_of(agent).get(to)
        if kind is None:
            raise ToolError(f"you are not allowed to contact {to}")
        limit = ctx.org.communication.max_messages
        if ctx.messages_sent[agent.name] >= limit:
            raise ToolError(f"you have used all {limit} of your messages")
        ctx.messages_sent[agent.name] += 1
        return ctx.agents[to], kind

    def _cc(self, sender: "Agent", target: "Agent", text: str) -> list[str]:
        ctx = self.ctx
        if not ctx.org.communication.cc_skipped:
            return []
        names = [n.name for n in skipped_managers(sender.node, target.node)]
        for n in names:
            ctx.inboxes[n].append(f"[cc] {sender.name} -> {target.name}: {text}")
        return names

    async def _t_send_message(self, agent: "Agent", args: dict) -> str:
        ctx = self.ctx
        target, kind = self._check_contact(agent, args["to"])
        text = args["text"][:4000]
        ctx.inboxes[target.name].append(f"[Message from {agent.name} ({agent.spec.role})]: {text}")
        cc = self._cc(agent, target, text)
        ctx.event(agent, "message", {"to": target.name, "kind": kind, "chars": len(text), "cc": cc,
                                     "preview": text[:240]})
        status = "" if target.name in ctx.active else f" ({target.name} is not working right now and will see it if they get another assignment.)"
        return f"Delivered to {target.name}'s inbox.{status}"

    async def _t_ask(self, agent: "Agent", args: dict) -> str:
        ctx = self.ctx
        target, kind = self._check_contact(agent, args["to"])
        question = args["question"][:4000]
        inverse, _ = relation(target.node, agent.node)
        provider = ctx.registry.provider_for(target.model)
        if target.conv is not None:
            conv = target.conv.fork()
        else:
            specs = tool_specs(target, ctx)
            conv = provider.new_conversation(system_prompt(target, ctx), specs)
        conv.add_user(question_message(agent, inverse, question))
        max_tokens = ctx.budget.plan_call(target.model, conv.approx_input_tokens(),
                                          min(target.spec.output_cap(), ANSWER_TOKENS))
        ctx.live(target, "answering", {"from": agent.name})
        gate = ctx.registry.gate_for(target.model)
        queued = await gate.acquire()
        try:
            result = await provider.step(conv, target.model, max_tokens=max_tokens, effort=target.spec.effort)
            if not result.text and result.tool_calls:  # it tried to use tools; ask once more with tools off
                ctx.budget.charge(target.model.cost(result.usage))
                ctx.log_call(target, result, target.model.cost(result.usage), -1, max_tokens, purpose="answer")
                conv.add_tool_results([ToolResult(c.id, "Tools are disabled here; answer in text.", True)
                                       for c in result.tool_calls])
                result = await provider.step(conv, target.model, max_tokens=max_tokens, effort=target.spec.effort,
                                             tool_choice="none")
        except ProviderError as e:
            raise ToolError(f"{target.name} could not answer: {e}")
        finally:
            gate.release()
            ctx.live(target, "working" if target.name in ctx.active else "idle", {})
        result.rate_wait_s += queued
        cost = target.model.cost(result.usage)
        ctx.budget.charge(cost)
        ctx.log_call(target, result, cost, -1, max_tokens, purpose="answer")
        answer = result.text.strip() or f"[{target.name} gave no answer]"
        ctx.inboxes[target.name].append(
            f"[FYI] In a side conversation, {agent.name} asked you: {question[:500]}\nYou answered: {answer[:800]}")
        cc = self._cc(agent, target, f"Q: {question[:500]} / A: {answer[:500]}")
        ctx.event(agent, "ask", {"to": target.name, "kind": kind, "chars": len(question), "answer_chars": len(answer),
                                 "cc": cc, "preview": question[:240], "answer_preview": answer[:240]})
        return f"Answer from {target.name}: {answer}"

    # ---- wikis ----
    def _wiki(self, agent: "Agent", wiki_id: str, write: bool = False):
        for inst, can_write in self.ctx.wiki_access(agent):
            if inst.id == wiki_id:
                if write and not can_write:
                    raise ToolError(f"you can read but not write {wiki_id}")
                return inst
        raise ToolError(f"no access to wiki {wiki_id}")

    async def _t_wiki_read(self, agent, args) -> str:
        inst = self._wiki(agent, args["wiki"])
        store = self.ctx.wiki
        title = (args.get("page") or "").strip()
        if not title:
            self.ctx.event(agent, "wiki_read", {"wiki": inst.id, "page": None})
            pages = list(store.pages.get(inst.id, {}).values())
            if not pages:
                return f"{inst.id} has no pages yet."
            return f"Pages in {inst.id}:\n" + "\n".join(
                f"- {p.title} ({p.byline()}; {len(p.content)} chars)" for p in pages)
        page = store.find(inst.id, title)
        if page is None:
            raise ToolError(f"no page {title!r} in {inst.id}; call wiki_read without a page to list pages")
        self.ctx.event(agent, "wiki_read", {"wiki": inst.id, "page": page.title})
        people = ", ".join(f"{a} ({role})" for a, role, _ in page.contributors())
        return f"# {page.title} ({page.byline()})\nContributors: {people}\n\n{truncate(page.content, MAX_READ)}"

    async def _t_wiki_search(self, agent, args) -> str:
        inst = self._wiki(agent, args["wiki"])
        hits = self.ctx.wiki.search(inst.id, args["query"])
        self.ctx.event(agent, "wiki_read", {"wiki": inst.id, "query": args["query"][:100]})
        if not hits:
            return "No matching pages."
        return "\n".join(f"- {p.title} ({p.current.author}, {p.current.role}): {p.content[:160]}..." for p in hits[:8])

    async def _t_wiki_write(self, agent, args) -> str:
        inst = self._wiki(agent, args["wiki"], write=True)
        store = self.ctx.wiki
        existing = store.find(inst.id, args["page"])
        new_len = len(args["content"]) + (len(existing.content) + 2 if existing and args.get("append") else 0)
        if new_len > MAX_WIKI_PAGE:
            raise ToolError(f"page too long ({new_len} chars; limit {MAX_WIKI_PAGE})")
        rev = store.write(inst.id, args["page"], args["content"], author=agent.name, role=agent.spec.role,
                          level=agent.level, model=agent.model.alias, append=bool(args.get("append")))
        title = store.find(inst.id, args["page"]).title
        self.ctx.event(agent, "wiki_write", {"wiki": inst.id, "page": title, "version": rev.version, "mode": rev.mode,
                                             "role": agent.spec.role, "chars": len(rev.content),
                                             "delta": rev.chars_delta, "preview": args["content"][:200]})
        return f"Saved {inst.id} / {title} (v{rev.version})."

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


__all__ = ["KIND_LABELS", "Toolbox", "ToolError", "tool_specs", "validate_args"]
