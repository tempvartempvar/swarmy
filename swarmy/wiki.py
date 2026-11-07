"""Wikis stored in git.

Each run's wikis live in one git repository (`runs/<run_id>/wiki/`), with a folder per wiki and one
Markdown file per page. Every edit is a commit:

    author   captain-1.2 (captain) <captain-1.2@swarmy.local>
    message  <edit summary>
             Swarmy-Wiki: unit-log/captain-1
             Swarmy-Page: Zone facts
             Swarmy-Role: captain
             Swarmy-Level: 2
             Swarmy-Model: deepseek-flash
             Swarmy-Mode: replace
             Swarmy-T: 12.3

So history, diffs and blame come from git itself, and humans can browse a run's wikis with any git
tool (`git -C results/runs/<run_id>/wiki log`, VS Code, GitHub...) as well as in the run explorer
and with `swarmy wiki`. Git access goes through dulwich (pure Python), so no git binary is needed.
Diffs use difflib; pages are rendered with markdown-it-py (see swarmy/markdown.py).
"""

from __future__ import annotations

import difflib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dulwich import porcelain
from dulwich.object_store import tree_lookup_path
from dulwich.repo import Repo

from .markdown import to_html
from .rules import WikiInstance
from .tasks.base import tokenize

META_FILE = ".swarmy/wikis.json"
COMMITTER = b"swarmy <swarmy@swarmy.local>"
TRAILER = re.compile(r"^Swarmy-([A-Za-z]+): ?(.*)$")


@dataclass
class Revision:
    version: int
    author: str  # position name, e.g. captain-1.2 (or "client")
    role: str  # rank / title, e.g. captain
    level: int | None  # depth in the hierarchy (0 = top)
    model: str | None
    mode: str  # create | replace | append
    t: float  # seconds since the run started
    chars_delta: int = 0
    summary: str = ""  # edit summary: what changed and why
    sha: str = ""  # git commit


@dataclass
class Page:
    title: str
    path: str  # path inside the repository
    content: str = ""
    revisions: list[Revision] = field(default_factory=list)

    @property
    def current(self) -> Revision:
        return self.revisions[-1]

    def contributors(self) -> list[tuple[str, str, int]]:
        """(author, role, number of edits), in order of first contribution."""
        seen: dict[str, list] = {}
        for r in self.revisions:
            seen.setdefault(r.author, [r.role, 0])[1] += 1
        return [(a, role, n) for a, (role, n) in seen.items()]

    def byline(self) -> str:
        cur = self.current
        lvl = f", level {cur.level}" if cur.level is not None else ""
        return f"v{cur.version}, last edited by {cur.author} ({cur.role}{lvl})"


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "page"


def _instance_meta(inst: WikiInstance) -> dict:
    return {"id": inst.id, "name": inst.spec.name, "scope": inst.spec.scope, "label": inst.label(),
            "owner": inst.owner, "description": inst.spec.description, "members": inst.members,
            "readers": sorted(inst.readers), "writers": sorted(inst.writers)}


class WikiStore:
    """The wikis of one run: an in-memory index of current pages over a git repository."""

    def __init__(self, instances: list[WikiInstance] | list[dict], root: Path, *, create: bool = True):
        self.root = Path(root).resolve()  # dulwich reads relative paths against the cwd, not the repo
        self.meta = [i if isinstance(i, dict) else _instance_meta(i) for i in instances]
        self.pages: dict[str, dict[str, Page]] = {m["id"]: {} for m in self.meta}
        self.t0 = time.time()
        self._lock = threading.RLock()
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            self.repo = Repo.init(str(self.root))
            meta_path = self.root / META_FILE
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps({"instances": self.meta, "t0": self.t0}, indent=1))
            (self.root / "README.md").write_text(
                "# Wikis of this run\n\nEach folder is one wiki (scoped to the whole org, a division or a unit); "
                "each Markdown file is a page. Every edit is a commit whose author is the agent and its rank, and "
                "whose message is the edit summary. Try `git log --format='%h %an: %s'`.\n")
            porcelain.add(self.repo, paths=[str(meta_path), str(self.root / "README.md")])
            self._commit(b"Initialize wikis", COMMITTER)
        else:
            self.repo = Repo(str(self.root))

    # ---- reading the index ----
    def find(self, wiki_id: str, title: str) -> Page | None:
        pages = self.pages.get(wiki_id, {})
        if title in pages:
            return pages[title]
        for cand in (title, re.sub(r"\s*\(v\d.*$", "", title.strip()).strip().strip('"\'')):
            low = cand.strip().lower()
            page = next((p for t, p in pages.items() if t.lower() == low), None)
            if page is not None:
                return page
        return None

    def search(self, wiki_id: str, query: str) -> list[Page]:
        terms = set(tokenize(query))
        scored = []
        for title, page in self.pages.get(wiki_id, {}).items():
            words = tokenize(title + " " + page.content)
            score = sum(words.count(t) for t in terms)
            if score:
                scored.append((score, title, page))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [p for _, _, p in scored]

    # ---- writing: one commit per edit ----
    def write(self, wiki_id: str, title: str, content: str, *, author: str, role: str, level: int | None,
              model: str | None, append: bool = False, summary: str = "") -> Revision:
        title = " ".join(title.split())[:120]  # one line: titles and summaries go into commit messages
        summary = " ".join(summary.split())[:300]
        with self._lock:
            page = self.find(wiki_id, title)
            if page is None:
                taken = {p.path for p in self.pages.get(wiki_id, {}).values()}
                path, n = f"{wiki_id}/{_slug(title)}.md", 2
                while path in taken:
                    path, n = f"{wiki_id}/{_slug(title)}-{n}.md", n + 1
                page = Page(title=title, path=path)
                self.pages.setdefault(wiki_id, {})[title] = page
                mode, new = "create", content
            elif append:
                mode, new = "append", page.content + "\n\n" + content
            else:
                mode, new = "replace", content
            prev = len(page.content) if page.revisions else 0
            t = round(time.time() - self.t0, 2)
            file = self.root / page.path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(new)
            porcelain.add(self.repo, paths=[str(file)])
            trailers = {"Wiki": wiki_id, "Page": page.title, "Role": role, "Level": "" if level is None else level,
                        "Model": model or "", "Mode": mode, "T": t}
            message = (summary or f"{mode.capitalize()} {page.title}") + "\n\n" + "\n".join(
                f"Swarmy-{k}: {v}" for k, v in trailers.items())
            who = f"{author} ({role}) <{re.sub(r'[^A-Za-z0-9._-]', '_', author)}@swarmy.local>".encode()
            sha = self._commit(message.encode(), who)
            rev = Revision(version=len(page.revisions) + 1, author=author, role=role, level=level, model=model,
                           mode=mode, t=t, chars_delta=len(new) - prev, summary=summary, sha=sha.decode())
            page.content = new
            page.revisions.append(rev)
            return rev

    def _commit(self, message: bytes, author: bytes) -> bytes:
        # sign/no_verify: never pick up the user's global commit signing or hooks for these internal commits
        return porcelain.commit(self.repo, message=message, author=author, committer=COMMITTER, sign=False,
                                no_verify=True)

    def close(self) -> None:
        """Release open pack files. Reading still works afterwards (dulwich reopens objects as needed)."""
        with self._lock:
            self.repo.close()

    # ---- reading history from git ----
    def _blob(self, sha: str, path: str) -> str:
        commit = self.repo[sha.encode()]
        _, blob = tree_lookup_path(self.repo.__getitem__, commit.tree, path.encode())
        return self.repo[blob].data.decode("utf-8", errors="replace")

    def revision_content(self, page: Page, version: int) -> str:
        with self._lock:
            return self._blob(page.revisions[version - 1].sha, page.path)

    def diff(self, wiki_id: str, title: str, version: int) -> list[str]:
        """Unified diff (difflib) of one revision against the previous one."""
        with self._lock:
            page = self.find(wiki_id, title)
            if page is None or not 1 <= version <= len(page.revisions):
                raise KeyError(f"{wiki_id}/{title} v{version}")
            new = self._blob(page.revisions[version - 1].sha, page.path)
            old = self._blob(page.revisions[version - 2].sha, page.path) if version > 1 else ""
        return unified_diff(old, new, version)

    def to_json(self, contents: bool = True) -> dict:
        with self._lock:
            out = []
            for m in self.meta:
                pages = []
                for p in self.pages.get(m["id"], {}).values():
                    revs, prev = [], ""
                    for r in p.revisions:
                        d = asdict(r)
                        if contents:
                            text = self._blob(r.sha, p.path)
                            d["content"], d["html"] = text, to_html(text)
                            d["diff"] = unified_diff(prev, text, r.version)
                            prev = text
                        revs.append(d)
                    pages.append({"title": p.title, "path": p.path, "revisions": revs})
                out.append({**m, "pages": pages})
            return {"instances": out, "storage": "git", "repo": str(self.root)}

    # ---- reopening a finished run ----
    @classmethod
    def open(cls, root: Path) -> "WikiStore":
        repo = Repo(str(root))
        head = repo.head()
        meta = json.loads(repo[tree_lookup_path(repo.__getitem__, repo[head].tree, META_FILE.encode())[1]].data)
        store = cls(meta["instances"], root, create=False)
        store.t0 = meta.get("t0", 0)
        for entry in repo.get_walker(reverse=True):
            c = entry.commit
            msg = c.message.decode("utf-8", errors="replace")
            tr = dict(m.groups() for line in msg.splitlines() if (m := TRAILER.match(line)))
            if "Wiki" not in tr:
                continue
            wiki_id, title = tr["Wiki"], tr["Page"]
            page = store.find(wiki_id, title)
            if page is None:
                changes = [ch for ch in entry.changes() if ch.new and ch.new.path]
                path = next((ch.new.path.decode() for ch in changes if ch.new.path.decode().startswith(wiki_id + "/")),
                            f"{wiki_id}/{_slug(title)}.md")
                page = Page(title=title, path=path)
                store.pages.setdefault(wiki_id, {})[title] = page
            content = store._blob(c.id.decode(), page.path)
            author = c.author.decode("utf-8", errors="replace").split(" <")[0]
            author = re.sub(r" \([^)]*\)$", "", author)
            page.revisions.append(Revision(
                version=len(page.revisions) + 1, author=author, role=tr.get("Role", ""),
                level=int(tr["Level"]) if tr.get("Level", "").isdigit() else None, model=tr.get("Model") or None,
                mode=tr.get("Mode", "replace"), t=float(tr.get("T") or 0), chars_delta=len(content) - len(page.content),
                summary=msg.split("\n\nSwarmy-")[0].strip() if tr.get("Mode") else "", sha=c.id.decode()))
            if page.revisions[-1].summary == f"{page.revisions[-1].mode.capitalize()} {title}":
                page.revisions[-1].summary = ""
            page.content = content
        return store


def _legacy_json(run_dir: Path) -> dict | None:
    """Runs from before git storage saved wikis/wikis.json."""
    f = run_dir / "wikis" / "wikis.json"
    if not f.exists():
        return None
    data = json.loads(f.read_text())
    for inst in data["instances"]:
        for page in inst["pages"]:
            prev = ""
            for r in page["revisions"]:
                r["html"] = to_html(r.get("content", ""))
                r["diff"] = unified_diff(prev, r.get("content", ""), r["version"])
                prev = r.get("content", "")
    data["storage"] = "json"
    return data


def open_store(run_dir: Path) -> WikiStore | None:
    root = run_dir / "wiki"
    return WikiStore.open(root) if (root / ".git").exists() else None


def load(run_dir: Path) -> dict | None:
    """All wikis of a finished run (every revision with its content, HTML and diff), or None."""
    store = open_store(run_dir)
    if store is None:
        return _legacy_json(run_dir)
    try:
        data = store.to_json()
    finally:
        store.close()
    return data if any(i["pages"] for i in data["instances"]) else None


def unified_diff(old: str, new: str, version: int) -> list[str]:
    """difflib's unified diff of two revisions (2 lines of context), without the ---/+++ header."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), f"v{version - 1}", f"v{version}", n=2,
                                 lineterm="")
    return [ln for ln in lines if not ln.startswith(("--- v", "+++ v"))]


def render_markdown(inst: dict, page: dict) -> str:
    """A page as Markdown with its contributors and revision history (for `swarmy wiki --page`)."""
    revs = page["revisions"]
    cur = revs[-1]
    people: dict[str, list] = {}
    for r in revs:
        people.setdefault(r["author"], [r["role"], 0])[1] += 1
    lines = [
        f"# {page['title']}",
        "",
        f"*Wiki `{inst['id']}` ({inst['label']}) · v{cur['version']} · last edited by **{cur['author']}** "
        f"({cur['role']}{', level ' + str(cur['level']) if cur['level'] is not None else ''}) at {cur['t']:.1f}s*",
        "",
        "Contributors: " + ", ".join(f"{a} ({role}, {n} edit{'s' if n > 1 else ''})" for a, (role, n) in people.items()),
        "",
        "---",
        "",
        cur.get("content", ""),
        "",
        "---",
        "",
        "## Revision history",
        "",
        "| v | when | author | rank | level | model | change | summary |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in revs:
        summary = (r.get("summary") or "").replace("|", "/")
        lines.append(f"| {r['version']} | {r['t']:.1f}s | {r['author']} | {r['role']} | "
                     f"{'' if r['level'] is None else r['level']} | {r['model'] or ''} | "
                     f"{r['mode']} ({r['chars_delta']:+d} chars) | {summary} |")
    return "\n".join(lines) + "\n"


def activity(tracer, run_id: str) -> dict:
    """How the run used its wikis: tool calls by type, failures with reasons, reminders."""
    rows = tracer.query("SELECT node, kind, data_json FROM events WHERE run_id=? AND kind IN "
                        "('tool', 'wiki_write', 'wiki_read', 'wiki_reminder') ORDER BY id", (run_id,))
    out = {"reads": 0, "writes": 0, "failed": [], "reminders": 0}
    for r in rows:
        d = json.loads(r["data_json"] or "{}")
        if r["kind"] == "wiki_read":
            out["reads"] += 1
        elif r["kind"] == "wiki_write":
            out["writes"] += 1
        elif r["kind"] == "wiki_reminder":
            out["reminders"] += 1
        elif str(d.get("name", "")).startswith("wiki") and not d.get("ok", True):
            out["failed"].append({"node": r["node"], "tool": d.get("name"), "error": d.get("error")})
    return out
