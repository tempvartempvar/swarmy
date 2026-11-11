"""Organization configs.

An org is a tree of positions. Write it either compactly as `levels` (every node in a tier
shares settings and has the same number of reports) or explicitly as a `tree` (any shape,
per-position models). Both normalize to the same `Node` tree, which is what the engine runs.

The config also defines how knowledge is shared (`knowledge.wikis`) and who may talk to whom
outside the delegate/report chain (`communication`).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from .ranks import SOLO_RANK, WORKER_RANK, management_ranks

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Audience = Literal["all", "managers", "workers"]

# Default output-token cap per call, by effort. Thinking counts against max_tokens,
# so high-effort positions need room. The run budget can clamp these further.
DEFAULT_MAX_TOKENS = {None: 12000, "low": 4000, "medium": 8000, "high": 16000, "xhigh": 24000, "max": 32000}
SETTING_FIELDS = ("role", "model", "effort", "goal", "max_turns", "max_tokens", "can_do_work", "review_tools", "prompt")


def auto_name(role: str, path: str) -> str:
    """Default position name: the title for the top position, else '<title>-<path>' (e.g. captain-1.2)."""
    slug = re.sub(r"\s+", "-", role.strip())
    return slug if not path else f"{slug}-{path}"


class NodeSettings(BaseModel):
    """What a position is: its title, model and limits."""

    model_config = ConfigDict(extra="forbid")
    role: str
    model: str
    effort: Effort | None = None
    goal: str = ""  # standing objective for this position, weighed alongside the orders it receives
    max_turns: int | None = None
    max_tokens: int | None = None
    can_do_work: bool = False  # managers: also get hands-on tools
    review_tools: bool = True  # managers: may inspect the workspace / run public tests
    prompt: str = ""  # extra role instructions appended to the system prompt

    def output_cap(self) -> int:
        return self.max_tokens or DEFAULT_MAX_TOKENS[self.effort]

    def settings(self) -> "NodeSettings":
        return NodeSettings(**{k: getattr(self, k) for k in SETTING_FIELDS})


class LevelSpec(NodeSettings):
    """One tier of a uniform hierarchy."""

    span: int = Field(0, ge=0, description="direct reports per node; 0 on the bottom level")


class TreeNode(NodeSettings):
    """One position in an explicit tree."""

    name: str | None = None
    children: list["TreeNode"] = []


class WikiSpec(BaseModel):
    """A shared knowledge base.

    scope org: one wiki for everyone. scope team: one wiki per team (a manager and their direct
    reports). scope department: one wiki per node at `level`, shared by that node's whole subtree.
    Commanders above a wiki's scope can always read it.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    scope: Literal["org", "department", "team"] = "org"
    level: int = Field(1, ge=0)
    read: Audience = "all"
    write: Audience = "all"
    publish_brief: bool = False  # post the client's original assignment on it
    description: str = ""


class Knowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wikis: list[WikiSpec] = []


class CommunicationRules(BaseModel):
    """Direct contact outside delegate/report. Everything is off by default (strict tree).

    skip_up = n lets an agent contact up to n levels above their own manager; skip_down = n lets a
    manager contact up to n levels below their direct reports. cc_skipped copies the managers
    in between on skip-level traffic.
    """

    model_config = ConfigDict(extra="forbid")
    chain: bool = False  # manager <-> direct report, mid-assignment
    peers: bool = False  # same manager
    cross_team: bool = False  # same level, different manager
    open: bool = False  # anyone may contact anyone
    skip_up: int = Field(0, ge=0)
    skip_down: int = Field(0, ge=0)
    cc_skipped: bool = True
    max_message_chars: int = Field(1500, ge=100)  # per send_message
    max_question_chars: int = Field(800, ge=100)  # per ask
    max_answer_tokens: int = Field(600, ge=100)  # answers to questions are short
    max_messages: int = Field(8, ge=0)  # per agent per run

    @property
    def enabled(self) -> bool:
        any_rule = self.chain or self.peers or self.cross_team or self.open or self.skip_up or self.skip_down
        return bool(any_rule and self.max_messages)


class CheckinRules(BaseModel):
    """What the client does when the top position stops.

    If it stops without submitting (turn limit, stopped calling tools, an error), or submits while
    the task's public tests still fail (verify_tests), it is asked whether the mission is finished
    and told to keep working if not, up to `max` times. A check-in only starts while at least
    `min_budget_frac` of the run budget and of the time limit is left.
    """

    model_config = ConfigDict(extra="forbid")
    max: int = Field(2, ge=0, le=10)
    verify_tests: bool = True
    min_budget_frac: float = Field(0.15, ge=0, le=1)


class ContextRules(BaseModel):
    """Keeping conversations small. When an agent's context passes max_tokens (estimated), older tool
    output, messages, reports and the bulky arguments of old tool calls (e.g. file contents it wrote)
    are cut to trim_to characters each; its orders and its last keep_turns turns stay intact. Trimming
    happens in one go when the limit is crossed (down to about 60% of it), so prompt caching keeps
    working between trims. A trim also drops the model's earlier reasoning: reasoning that providers
    sign is only valid on an unedited history."""

    model_config = ConfigDict(extra="forbid")
    max_tokens: int = Field(30000, ge=2000)
    keep_turns: int = Field(4, ge=1)
    trim_to: int = Field(400, ge=100)


class OrderRules(BaseModel):
    """How commanders must write orders.

    Every order has an objective, distilled context, deliverables, constraints and a done-when test
    (a simplified military orders format). Orders run in the background: `delegate` returns at once
    and reports come back as subordinates finish. Length limits: max_chars for orders to commanders,
    max_chars_worker for orders to hands-on workers (one well-scoped task), max_report_chars for
    reports going up. no_micromanaging rejects orders to a commander that name positions below it (the
    commander decides how to split its own work); no_forwarding rejects orders and wiki edits that
    paste long passages of the writer's own orders.
    """

    model_config = ConfigDict(extra="forbid")
    max_chars: int = Field(3000, ge=200)
    max_chars_worker: int = Field(1800, ge=200)
    max_report_chars: int = Field(3000, ge=300)
    no_micromanaging: bool = True
    no_forwarding: bool = True


class Node:
    """Normalized position used by the engine and the UI."""

    def __init__(self, name: str, depth: int, path: str, settings: NodeSettings, parent: "Node | None"):
        self.name = name
        self.depth = depth
        self.path = path
        self.settings = settings
        self.parent = parent
        self.children: list[Node] = []

    @property
    def role(self) -> str:
        return self.settings.role

    @property
    def is_manager(self) -> bool:
        return bool(self.children)

    def walk(self) -> Iterator["Node"]:
        yield self
        for c in self.children:
            yield from c.walk()

    def ancestors(self) -> list["Node"]:
        out, cur = [], self.parent
        while cur is not None:
            out.append(cur)
            cur = cur.parent
        return out


class OrgConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    levels: list[LevelSpec] | None = None
    tree: TreeNode | None = None
    knowledge: Knowledge = Knowledge()
    communication: CommunicationRules = CommunicationRules()
    orders: OrderRules = OrderRules()
    checkins: CheckinRules = CheckinRules()
    context: ContextRules = ContextRules()
    manager_max_turns: int = 12
    worker_max_turns: int = 24
    _root: Node | None = PrivateAttr(None)

    @model_validator(mode="after")
    def _check(self) -> "OrgConfig":
        if (self.levels is None) == (self.tree is None):
            raise ValueError("an org needs exactly one of `levels` or `tree`")
        if self.levels is not None:
            if not self.levels:
                raise ValueError("an org needs at least one level")
            for i, lvl in enumerate(self.levels[:-1]):
                if lvl.span < 1:
                    raise ValueError(f"level {i} ({lvl.role}) needs span >= 1")
            if self.levels[-1].span != 0:
                raise ValueError("the bottom level must have span 0")
        self._root = self._build_root()
        names = [n.name for n in self._root.walk()]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"position names must be unique: {dupes}")
        wnames = [w.name for w in self.knowledge.wikis]
        if len(set(wnames)) != len(wnames):
            raise ValueError(f"wiki names must be unique: {wnames}")
        for w in self.knowledge.wikis:
            if w.scope == "department" and w.level > self.depth:
                raise ValueError(f"wiki {w.name!r}: department level {w.level} is below the deepest level")
        return self

    def _build_root(self) -> Node:
        if self.levels is not None:
            def make_level(i: int, parent: Node | None, path: str) -> Node:
                lvl = self.levels[i]
                name = auto_name(lvl.role, path)
                node = Node(name, i, path, lvl.settings(), parent)
                if i < len(self.levels) - 1:
                    for k in range(lvl.span):
                        node.children.append(make_level(i + 1, node, f"{path}.{k + 1}" if path else str(k + 1)))
                return node
            return make_level(0, None, "")

        def make_tree(t: TreeNode, depth: int, parent: Node | None, path: str) -> Node:
            name = t.name or auto_name(t.role, path)
            node = Node(name, depth, path, t.settings(), parent)
            for k, c in enumerate(t.children):
                node.children.append(make_tree(c, depth + 1, node, f"{path}.{k + 1}" if path else str(k + 1)))
            return node
        return make_tree(self.tree, 0, None, "")

    # ---- normalized structure ----
    @property
    def root(self) -> Node:
        return self._root

    def nodes(self) -> list[Node]:
        return list(self._root.walk())

    @property
    def layers(self) -> list[list[Node]]:
        out: list[list[Node]] = []
        for n in self._root.walk():
            while len(out) <= n.depth:
                out.append([])
            out[n.depth].append(n)
        return out

    @property
    def counts(self) -> list[int]:
        return [len(layer) for layer in self.layers]

    @property
    def depth(self) -> int:
        """Management layers above the deepest workers (0 = a single agent)."""
        return len(self.layers) - 1

    @property
    def n_agents(self) -> int:
        return sum(self.counts)

    @property
    def n_workers(self) -> int:
        return sum(1 for n in self._root.walk() if not n.children)

    @property
    def n_managers(self) -> int:
        return self.n_agents - self.n_workers

    @property
    def shape(self) -> str:
        return "-".join(str(c) for c in self.counts)

    def metrics(self) -> dict[str, Any]:
        spans = [len(n.children) for n in self._root.walk() if n.children]
        return {
            "depth": self.depth,
            "levels": len(self.layers),
            "shape": self.shape,
            "n_agents": self.n_agents,
            "n_workers": self.n_workers,
            "n_managers": self.n_managers,
            "manager_ratio": round(self.n_managers / self.n_workers, 3),
            "avg_span": round((self.n_agents - 1) / self.n_managers, 3) if self.n_managers else 0,
            "max_span": max(spans) if spans else 0,
            "uniform": self.is_uniform(),
            "wikis": len(self.knowledge.wikis),
            "communication": self.communication_summary(),
        }

    def communication_summary(self) -> str:
        c = self.communication
        if not c.enabled:
            return "tree"
        parts = [k for k in ("chain", "peers", "cross_team", "open") if getattr(c, k)]
        if c.skip_up:
            parts.append(f"skip_up={c.skip_up}")
        if c.skip_down:
            parts.append(f"skip_down={c.skip_down}")
        return "+".join(parts)

    def layer_roles(self) -> list[str]:
        """One label per depth, e.g. 'director' or 'director/manager' for mixed layers."""
        return ["/".join(dict.fromkeys(n.role for n in layer)) for layer in self.layers]

    def describe(self) -> str:
        parts = []
        for layer in self.layers:
            roles = "/".join(dict.fromkeys(n.role for n in layer))
            models = Counter(f"{n.settings.model}{'/' + n.settings.effort if n.settings.effort else ''}" for n in layer)
            label = ", ".join(m if len(models) == 1 else f"{m} x{c}" for m, c in models.items())
            parts.append(f"{roles} x{len(layer)} [{label}]")
        return " -> ".join(parts)

    def is_uniform(self) -> bool:
        """True if the tree can be written as `levels`."""
        if self.levels is not None:
            return True
        for i, layer in enumerate(self.layers):
            first = layer[0]
            spans = {len(n.children) for n in layer}
            if len(spans) != 1:
                return False
            if any(n.settings != first.settings for n in layer):
                return False
            if any(n.name != auto_name(n.role, n.path) for n in layer):
                return False
        roles = [layer[0].role for layer in self.layers]
        return len(set(roles)) == len(roles)

    # ---- serialization ----
    def tree_dict(self) -> dict:
        """Explicit tree form (what the UI edits)."""
        def dump(n: Node) -> dict:
            d = n.settings.model_dump(exclude_defaults=True)
            d["role"], d["model"] = n.role, n.settings.model
            if n.name != auto_name(n.role, n.path):
                d["name"] = n.name
            if n.children:
                d["children"] = [dump(c) for c in n.children]
            return d
        return dump(self._root)

    def compact_dict(self) -> dict:
        """Config dict using `levels` when the tree is uniform, else `tree`."""
        base = self.model_dump(exclude={"levels", "tree"}, exclude_defaults=True)
        base["name"] = self.name
        if self.is_uniform():
            levels = []
            for layer in self.layers:
                d = layer[0].settings.model_dump(exclude_defaults=True)
                d["role"], d["model"] = layer[0].role, layer[0].settings.model
                if layer[0].children:
                    d["span"] = len(layer[0].children)
                levels.append(d)
            base["levels"] = levels
        else:
            base["tree"] = self.tree_dict()
        return base

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.compact_dict(), sort_keys=False, allow_unicode=True)

    # ---- overrides ----
    def with_overrides(
        self,
        models: dict[str, str] | None = None,
        efforts: dict[str, str | None] | None = None,
        sets: list[str] | None = None,
        name: str | None = None,
    ) -> "OrgConfig":
        """Return a copy with per-role model/effort swaps and dotted-path overrides.

        models={"worker": "qwen-flash"}; efforts={"ceo": "high"}; roles "top" and "bottom" also work.
        sets=["levels.0.span=3", "communication.peers=true"] edit the raw config by path.
        """
        data = self.model_dump(exclude_none=False)
        data = {k: v for k, v in data.items() if not (k in ("levels", "tree") and v is None)}
        suffix = []
        for field, mapping in (("model", models or {}), ("effort", efforts or {})):
            for role, value in mapping.items():
                targets = self._targets(data, role)
                if not targets:
                    roles = sorted({n.role for n in self._root.walk()})
                    raise KeyError(f"org {self.name!r} has no role {role!r} (roles: {roles})")
                for t in targets:
                    t[field] = value
                suffix.append(f"{role}={value}" if field == "model" else f"{role}.effort={value}")
        for item in sets or []:
            key, _, raw = item.partition("=")
            _set_path(data, key.strip(), yaml.safe_load(raw))
            suffix.append(item)
        if name:
            data["name"] = name
        elif suffix:
            data["name"] = f"{self.name}[{','.join(suffix)}]"
        return OrgConfig.model_validate(data)

    @staticmethod
    def _targets(data: dict, role: str) -> list[dict]:
        if data.get("levels") is not None:
            levels = data["levels"]
            if role == "top":
                return [levels[0]]
            if role == "bottom":
                return [levels[-1]]
            return [lvl for lvl in levels if lvl["role"] == role]
        out = []

        def walk(node: dict, depth: int) -> None:
            kids = node.get("children") or []
            if (role == "top" and depth == 0) or (role == "bottom" and not kids) or node.get("role") == role:
                out.append(node)
            for c in kids:
                walk(c, depth + 1)
        walk(data["tree"], 0)
        return out


def _set_path(data: Any, path: str, value: Any) -> None:
    keys = path.split(".")
    cur = data
    for k in keys[:-1]:
        cur = cur[int(k)] if isinstance(cur, list) else cur.setdefault(k, {})
    last = keys[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def load_org(path: str | Path) -> OrgConfig:
    data = yaml.safe_load(Path(path).read_text())
    return OrgConfig.model_validate(data)


def org_from_json(text: str | None) -> OrgConfig | None:
    """The org saved with a run, or None. Options that have since been removed are dropped, so runs
    recorded by older versions still open."""
    if not text:
        return None
    data = json.loads(text)
    while True:
        try:
            return OrgConfig.model_validate(data)
        except ValidationError as e:
            errors = e.errors()
            if any(err["type"] != "extra_forbidden" for err in errors):
                return None
            for err in errors:
                *path, key = [k for k in err["loc"] if not str(k).startswith("function-")]
                parent = data
                for k in path:
                    parent = parent[k]
                parent.pop(key, None)


# ---------------------------------------------------------------------------
# Topology generation: same worker count, different depth.
# ---------------------------------------------------------------------------

def balanced_spans(workers: int, depth: int) -> list[int]:
    """Split `workers` into `depth` spans whose product is `workers`, as even as possible.

    Larger spans go to the lower levels. If there are not enough prime factors,
    the top levels get span 1 (a pure relay layer).
    """
    if depth < 1:
        raise ValueError("depth must be >= 1 for a multi-level org")
    primes, n, p = [], workers, 2
    while n > 1:
        while n % p == 0:
            primes.append(p)
            n //= p
        p += 1
    bins = [1] * depth
    for prime in sorted(primes, reverse=True):
        bins[bins.index(min(bins))] *= prime
    return sorted(bins)


class LevelTemplate(NodeSettings):
    role: str | None = None  # default: the rank for that depth

    def level(self, role: str, span: int = 0) -> LevelSpec:
        return LevelSpec(**{**self.model_dump(), "role": role, "span": span})


class GenerateSpec(BaseModel):
    """Generate a family of orgs with a fixed number of workers and varying depth.

    `ladder` lists management tiers from the top down (e.g. ceo, c-suite, director, manager).
    For depth d, the top tier is always kept and the remaining d-1 tiers are taken from the
    bottom of the ladder, so flattening removes middle management first. Set `ladder` to a
    single template to give every management tier the same model (structure-only control).
    Depth 0 is the single-agent baseline: the top template working alone.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    workers: int
    depths: list[int] = Field(min_length=1)
    ladder: list[LevelTemplate]
    worker: LevelTemplate
    spans: dict[int, list[int]] = {}  # optional explicit spans per depth
    communication: dict = {}
    knowledge: dict = {}

    def build(self) -> list[OrgConfig]:
        orgs = []
        for d in self.depths:
            if d == 0:
                top = self.ladder[0]
                orgs.append(OrgConfig(name=f"{self.name}-solo", levels=[top.level(top.role or SOLO_RANK)]))
                continue
            spans = self.spans.get(d) or balanced_spans(self.workers, d)
            if len(self.ladder) == 1:
                tiers = [self.ladder[0]] * d
            elif d <= len(self.ladder):
                tiers = [self.ladder[0]] + self.ladder[len(self.ladder) - (d - 1):] if d > 1 else [self.ladder[0]]
            else:
                tiers = [self.ladder[0]] + [self.ladder[-1]] * (d - 1)
            levels = []
            ranks = management_ranks(d)
            for i, (tpl, span) in enumerate(zip(tiers, spans)):
                role = tpl.role or ranks[i]
                if any(lv.role == role for lv in levels):
                    role = ranks[i] if not any(lv.role == ranks[i] for lv in levels) else f"{role}-{i}"
                levels.append(tpl.level(role, span))
            levels.append(self.worker.level(self.worker.role or WORKER_RANK))
            orgs.append(OrgConfig.model_validate({
                "name": f"{self.name}-d{d}",
                "description": f"generated: {self.workers} workers, depth {d}",
                "levels": [lv.model_dump() for lv in levels],
                "communication": self.communication, "knowledge": self.knowledge,
            }))
        return orgs
