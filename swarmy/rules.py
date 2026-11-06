"""Who may contact whom, and who can read/write which wiki.

The engine and the UI both use these functions, so the chart always shows exactly the rules
the agents run under.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Node, OrgConfig, WikiSpec

KIND_LABELS = {
    "manager": "your manager",
    "report": "your direct report",
    "peer": "a teammate (same manager)",
    "skip_up": "skip-level, above your manager",
    "skip_down": "skip-level, below your direct reports",
    "cross_team": "same level, another team",
    "other": "elsewhere in the organization",
}


def relation(a: Node, b: Node) -> tuple[str, int]:
    """How b relates to a, and the distance in levels for ancestor/descendant relations."""
    if b.parent is a:
        return "report", 1
    if a.parent is b:
        return "manager", 1
    if a.parent is not None and a.parent is b.parent:
        return "peer", 0
    anc_a = a.ancestors()
    if b in anc_a:
        return "skip_up", anc_a.index(b) + 1
    anc_b = b.ancestors()
    if a in anc_b:
        return "skip_down", anc_b.index(a) + 1
    if a.depth == b.depth:
        return "cross_team", 0
    return "other", 0


def allowed(org: OrgConfig, a: Node, b: Node) -> str | None:
    """The kind of contact a may make with b under the org's rules, or None."""
    if a is b:
        return None
    c = org.communication
    if not c.enabled:
        return None
    kind, dist = relation(a, b)
    if c.open:
        return kind
    ok = {
        "manager": c.chain,
        "report": c.chain,
        "peer": c.peers,
        "cross_team": c.cross_team,
        "skip_up": dist - 1 <= c.skip_up,
        "skip_down": dist - 1 <= c.skip_down,
        "other": False,
    }[kind]
    return kind if ok else None


def contacts(org: OrgConfig, a: Node) -> list[tuple[Node, str]]:
    return [(b, k) for b in org.root.walk() if (k := allowed(org, a, b))]


def all_contacts(org: OrgConfig) -> dict[str, list[dict]]:
    return {a.name: [{"to": b.name, "kind": k} for b, k in contacts(org, a)] for a in org.root.walk()}


def skipped_managers(a: Node, b: Node) -> list[Node]:
    """Managers strictly between a and b on a skip-level path (they get cc'd)."""
    kind, _ = relation(a, b)
    if kind == "skip_up":
        chain = a.ancestors()
        return chain[: chain.index(b)]
    if kind == "skip_down":
        chain = b.ancestors()
        return chain[: chain.index(a)]
    return []


# ---------------------------------------------------------------------------
# Wikis
# ---------------------------------------------------------------------------

@dataclass
class WikiInstance:
    id: str
    spec: WikiSpec
    owner: str | None  # team lead / department head (None for org-wide)
    members: list[str] = field(default_factory=list)
    readers: set[str] = field(default_factory=set)
    writers: set[str] = field(default_factory=set)

    def label(self) -> str:
        if self.spec.scope == "org":
            return "whole organization"
        if self.spec.scope == "team":
            return f"unit led by {self.owner}"
        return f"division under {self.owner}"


def _audience(nodes: list[Node], who: str) -> set[str]:
    if who == "managers":
        return {n.name for n in nodes if n.children}
    if who == "workers":
        return {n.name for n in nodes if not n.children}
    return {n.name for n in nodes}


def wiki_instances(org: OrgConfig) -> list[WikiInstance]:
    out: list[WikiInstance] = []
    for spec in org.knowledge.wikis:
        groups: list[tuple[str | None, list[Node], list[Node]]] = []  # (owner, members, overseers)
        if spec.scope == "org" or (spec.scope == "department" and spec.level == 0):
            groups.append((None, list(org.root.walk()), []))
        elif spec.scope == "team":
            for m in org.root.walk():
                if m.children:
                    groups.append((m.name, [m] + m.children, m.ancestors()))
        else:
            for n in org.layers[spec.level]:
                groups.append((n.name, list(n.walk()), n.ancestors()))
        for owner, members, overseers in groups:
            inst = WikiInstance(id=spec.name if owner is None else f"{spec.name}/{owner}", spec=spec, owner=owner,
                                members=[n.name for n in members])
            inst.readers = _audience(members, spec.read)
            inst.writers = _audience(members, spec.write)
            if spec.oversight:
                inst.readers |= {n.name for n in overseers}
            out.append(inst)
    return out


def wikis_for(instances: list[WikiInstance], name: str) -> list[tuple[WikiInstance, bool]]:
    """Wikis an agent can read, with whether it can also write."""
    return [(w, name in w.writers) for w in instances if name in w.readers or name in w.writers]
