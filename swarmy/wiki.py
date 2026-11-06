"""Wiki storage with full revision history.

Every edit is kept as a revision recording who made it: their name, rank (role), level and
model, plus the time into the run. Agents see the latest version along with its contributors.
Humans can browse everything afterwards: `wikis/wikis.json` plus one Markdown file per page in
each run directory, `swarmy wiki <run>` on the command line, and the wiki browser in `swarmy ui`.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .rules import WikiInstance
from .tasks.base import tokenize


@dataclass
class Revision:
    version: int
    author: str  # position name, e.g. capitaine-1.2 (or "client")
    role: str  # rank / title, e.g. capitaine
    level: int | None  # depth in the hierarchy (0 = top)
    model: str | None
    mode: str  # create | replace | append
    content: str
    t: float  # seconds since the run started
    chars_delta: int = 0


@dataclass
class Page:
    title: str
    revisions: list[Revision] = field(default_factory=list)

    @property
    def current(self) -> Revision:
        return self.revisions[-1]

    @property
    def content(self) -> str:
        return self.current.content

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


class WikiStore:
    def __init__(self, instances: list[WikiInstance]):
        self.instances = {i.id: i for i in instances}
        self.pages: dict[str, dict[str, Page]] = {i.id: {} for i in instances}
        self.t0 = time.time()
        self._lock = threading.Lock()

    def find(self, wiki_id: str, title: str) -> Page | None:
        pages = self.pages.get(wiki_id, {})
        if title in pages:
            return pages[title]
        low = title.strip().lower()
        return next((p for t, p in pages.items() if t.lower() == low), None)

    def write(self, wiki_id: str, title: str, content: str, *, author: str, role: str, level: int | None,
              model: str | None, append: bool = False) -> Revision:
        with self._lock:
            page = self.find(wiki_id, title)
            if page is None:
                page = Page(title=title.strip()[:120])
                self.pages.setdefault(wiki_id, {})[page.title] = page
                mode, new = "create", content
            elif append:
                mode, new = "append", page.content + "\n\n" + content
            else:
                mode, new = "replace", content
            prev = len(page.content) if page.revisions else 0
            rev = Revision(version=len(page.revisions) + 1, author=author, role=role, level=level, model=model,
                           mode=mode, content=new, t=round(time.time() - self.t0, 2), chars_delta=len(new) - prev)
            page.revisions.append(rev)
            return rev

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

    # ---- export ----
    def to_json(self) -> dict:
        with self._lock:
            instances = []
            for inst in self.instances.values():
                pages = list(self.pages.get(inst.id, {}).values())
                instances.append({
                    "id": inst.id, "name": inst.spec.name, "scope": inst.spec.scope, "label": inst.label(),
                    "owner": inst.owner, "description": inst.spec.description, "members": inst.members,
                    "readers": sorted(inst.readers), "writers": sorted(inst.writers),
                    "pages": [{"title": p.title, "revisions": [asdict(r) for r in p.revisions]} for p in pages],
                })
            return {"instances": instances}

    def save(self, out: Path) -> None:
        data = self.to_json()
        if not any(i["pages"] for i in data["instances"]):
            return
        out.mkdir(parents=True, exist_ok=True)
        (out / "wikis.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
        for inst in data["instances"]:
            if not inst["pages"]:
                continue
            d = out / inst["id"].replace("/", "__")
            d.mkdir(parents=True, exist_ok=True)
            for i, page in enumerate(inst["pages"]):
                (d / f"{_safe(page['title'], i)}.md").write_text(render_markdown(inst, page))


def _safe(title: str, i: int) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in title)[:80].strip() or f"page{i}"


def render_markdown(inst: dict, page: dict) -> str:
    """A page as Markdown with a header naming its editors and a revision log."""
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
        cur["content"],
        "",
        "---",
        "",
        "## Revision history",
        "",
        "| v | when | author | rank | level | model | change |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in revs:
        lines.append(f"| {r['version']} | {r['t']:.1f}s | {r['author']} | {r['role']} | "
                     f"{'' if r['level'] is None else r['level']} | {r['model'] or ''} | "
                     f"{r['mode']} ({r['chars_delta']:+d} chars) |")
    return "\n".join(lines) + "\n"


def load(run_dir: Path) -> dict | None:
    f = run_dir / "wikis" / "wikis.json"
    return json.loads(f.read_text()) if f.exists() else None
