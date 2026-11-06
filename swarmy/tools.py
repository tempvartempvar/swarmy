"""Tools available to agents. Which tools an agent gets depends on its position (manager or
hands-on worker), its settings, the task, the wikis it can access and the org's communication
rules."""

from __future__ import annotations

import asyncio
import os
import re
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


ORDER_FIELDS = [  # (key, heading, required, max chars, description)
    ("objective", "OBJECTIVE", True, 500, "one or two sentences: the single outcome this subordinate must achieve"),
    ("context", "CONTEXT", False, 2000, "only the facts and background they need, distilled in your own words "
                                         "(point to wiki pages or files instead of repeating them)"),
    ("deliverables", "DELIVERABLES", True, 800, "exactly what to hand back: files, facts, decisions, and in what form"),
    ("constraints", "CONSTRAINTS", False, 1200, "interfaces, limits, rules, what not to do"),
    ("done_when", "DONE WHEN", True, 600, "the test that shows the task is finished"),
]
FORWARD_LIMIT = 300  # verbatim characters of your own orders an order or wiki edit may repeat


class ToolError(Exception):
    pass


def forwarded_chars(new: str, source: str, min_len: int = 60) -> int:
    """Characters of `new` that are long verbatim fragments (sentences/lines) of `source`."""
    if not source:
        return 0
    total = 0
    for frag in re.split(r"(?<=[.!?:;])\s+|\n+", new):
        f = frag.strip()
        if len(f) >= min_len and f in source:
            total += len(f)
    return total


def render_order(fields: dict) -> str:
    parts = []
    for key, heading, _, _, _ in ORDER_FIELDS:
        val = (fields.get(key) or "").strip()
        if val:
            parts.append(f"{heading}\n{val}")
    return "\n\n".join(parts)


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


def tool_specs(agent: "Agent", ctx: "RunContext") -> list[ToolSpec]:
    task, org = ctx.task, ctx.org
    specs: list[ToolSpec] = []
    hands_on = not agent.children or agent.spec.can_do_work
    review = bool(agent.children) and agent.spec.review_tools

    if agent.children:
        names = [c.name for c in agent.children]
        rules = org.orders
        to_field = {"type": "string", "enum": names, "description": "name of the direct subordinate"}
        if rules.format == "structured":
            item = _obj({"to": to_field, **{k: {"type": "string", "description": d} for k, _, _, _, d in ORDER_FIELDS}},
                        ["to"] + [k for k, _, req, _, _ in ORDER_FIELDS if req])
            how = ("Each order is a well-constrained task: one objective, only the context that subordinate needs "
                   "(distilled, in your own words), concrete deliverables, constraints and a done-when test.")
        else:
            item = _obj({"to": to_field, "task": {"type": "string", "description": "complete, self-contained instructions"}},
                        ["to", "task"])
            how = "Each order must be self-contained."
        forward = " Orders that repeat your own orders verbatim are rejected." if rules.no_forwarding else ""
        specs.append(ToolSpec(
            "delegate",
            "Give orders to one or more of your direct subordinates. All orders in one call run in parallel; the "
            f"tool returns when every subordinate has reported back. The subordinate sees only what you write. {how}"
            f" Keep each order under {rules.max_chars} characters.{forward}",
            _obj({"assignments": {"type": "array", "items": item}}, ["assignments"]),
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
            specs.append(ToolSpec(
                "wiki_write",
                "Create or improve a topic page on a shared wiki. Wikis are a curated knowledge base: one page per "
                "topic, distilled facts and decisions with their sources, kept current. Read the page first, then "
                "write the whole improved page (it replaces the old version). Use append=true only for log-style "
                "pages. Never paste orders or whole documents.",
                _obj({"wiki": {"type": "string", "enum": writable},
                      "page": {"type": "string", "description": "topic title, e.g. 'Zone facts' or 'Interfaces'"},
                      "content": {"type": "string", "description": "the full page (Markdown)"},
                      "summary": {"type": "string", "description": "edit summary: what changed and why (recommended)"},
                      "append": {"type": "boolean"}},
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

    def _reject(self, agent: "Agent", call: ToolCall, message: str) -> ToolResult:
        # every failed call is logged, so the activity feed and diagnostics can show it
        self.ctx.event(agent, "tool", {"name": call.name, "ok": False, "error": message[:300]})
        return ToolResult(call.id, message, True)

    async def execute(self, agent: "Agent", call: ToolCall, specs: dict[str, ToolSpec]) -> ToolResult:
        if call.error:
            return self._reject(agent, call, f"Tool call rejected: {call.error}")
        spec = specs.get(call.name)
        if spec is None:
            return self._reject(agent, call, f"Unknown tool '{call.name}'. Available: {', '.join(specs)}")
        problem = validate_args(spec, call.arguments)
        if problem:
            return self._reject(agent, call, f"Invalid arguments for {call.name}: {problem}")
        try:
            handler = getattr(self, f"_t_{call.name}")
            out = await handler(agent, call.arguments)
            self.ctx.event(agent, "tool", {"name": call.name, "ok": True})
            return ToolResult(call.id, out)
        except ToolError as e:
            self.ctx.event(agent, "tool", {"name": call.name, "ok": False, "error": str(e)[:200]})
            return ToolResult(call.id, f"Error: {e}", True)

    # ---- delegation ----
    def _order_from(self, agent: "Agent", a: dict) -> tuple[str, dict]:
        """Validate one assignment and return (rendered order text, structured fields)."""
        rules = self.ctx.org.orders
        if rules.format == "structured":
            fields = {k: str(a.get(k) or "").strip() for k, _, _, _, _ in ORDER_FIELDS}
            if not any(fields.values()) and a.get("task"):
                raise ToolError("write a structured order (objective, context, deliverables, constraints, done_when), "
                                "not a single task string")
            missing = [k for k, _, req, _, _ in ORDER_FIELDS if req and not fields[k]]
            if missing:
                raise ToolError(f"missing {', '.join(missing)}")
            for k, _, _, limit, _ in ORDER_FIELDS:
                if len(fields[k]) > limit:
                    raise ToolError(f"{k} is {len(fields[k])} characters; keep it under {limit}. Distill it.")
            text = render_order(fields)
        else:
            text, fields = str(a.get("task") or "").strip(), {}
            if not text:
                raise ToolError("empty task")
        if len(text) > rules.max_chars:
            raise ToolError(f"order is {len(text)} characters; the limit is {rules.max_chars}. Give only what this "
                            "subordinate needs; put shared facts on a wiki page and point to it.")
        if rules.no_forwarding:
            copied = forwarded_chars(text, agent.current_order_text)
            if copied > FORWARD_LIMIT or (copied > 120 and copied > 0.5 * len(text)):
                raise ToolError(f"this order repeats {copied} characters of your own orders verbatim. Don't relay: "
                                "decide what this subordinate must do and give it only the facts it needs, in your "
                                "own words.")
        return text, fields

    async def _t_delegate(self, agent: "Agent", args: dict) -> str:
        ctx = self.ctx
        by_name = {c.name: c for c in agent.children}
        jobs: dict[str, str] = {}
        job_fields: dict[str, dict] = {}
        problems = []
        for a in args.get("assignments") or []:
            if not isinstance(a, dict):
                problems.append("each assignment must be an object")
                continue
            to = a.get("to")
            if to not in by_name:
                problems.append(f"'{to}' is not one of your direct subordinates ({', '.join(by_name)})")
                continue
            if to in jobs:
                problems.append(f"only one order per subordinate per call ({to} got two; the first was kept)")
                continue
            try:
                jobs[to], job_fields[to] = self._order_from(agent, a)
            except ToolError as e:
                problems.append(f"order to {to} rejected: {e}")
        if not jobs:
            raise ToolError("no orders were sent. " + "; ".join(problems))

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
                "fields": job_fields.get(to) or None,
            })

        ctx.live(agent, "waiting", {"on": list(jobs)})
        async with asyncio.TaskGroup() as tg:
            futures = {to: tg.create_task(by_name[to].handle(assignment_message(agent, text), ctx,
                                                             order_id=order_ids[to]))
                       for to, text in jobs.items()}
        ctx.live(agent, "working", {})

        parts = [f"Note: {p}. Fix and resend that order if it is still needed." for p in problems]
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
                f"- {p.title} ({p.byline()}; {len(p.content)} chars)"
                + (f" - last change: {p.current.summary}" if p.current.summary else "") for p in pages)
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
            raise ToolError(f"page too long ({new_len} chars; limit {MAX_WIKI_PAGE}). Distill it or split the topic.")
        if self.ctx.org.orders.no_forwarding:
            copied = forwarded_chars(args["content"], agent.current_order_text)
            if copied > FORWARD_LIMIT:
                raise ToolError(f"this edit pastes {copied} characters of your orders. Wiki pages hold distilled "
                                "knowledge (facts, decisions, interfaces, results), not orders.")
        if existing and not args.get("append") and existing.content.strip() == args["content"].strip():
            return f"No change: {inst.id} / {existing.title} already has this content."
        agent.wiki_writes += 1
        rev = store.write(inst.id, args["page"], args["content"], author=agent.name, role=agent.spec.role,
                          level=agent.level, model=agent.model.alias, append=bool(args.get("append")),
                          summary=str(args.get("summary") or ""))
        title = store.find(inst.id, args["page"]).title
        self.ctx.event(agent, "wiki_write", {"wiki": inst.id, "page": title, "version": rev.version, "mode": rev.mode,
                                             "role": agent.spec.role, "chars": len(rev.content),
                                             "delta": rev.chars_delta, "summary": rev.summary,
                                             "preview": args["content"][:200]})
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
