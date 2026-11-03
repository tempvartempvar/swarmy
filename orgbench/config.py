"""Organization configs: levels, spans of control, model per level, communication rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Effort = Literal["low", "medium", "high", "xhigh", "max"]

# Default output-token cap per call, by effort. Thinking counts against max_tokens,
# so high-effort levels need room. The run budget can clamp these further.
DEFAULT_MAX_TOKENS = {None: 12000, "low": 4000, "medium": 8000, "high": 16000, "xhigh": 24000, "max": 32000}


class LevelSpec(BaseModel):
    """One tier of the hierarchy. Every node at this tier shares these settings."""

    model_config = ConfigDict(extra="forbid")
    role: str
    model: str
    effort: Effort | None = None
    span: int = Field(0, ge=0, description="direct reports per node; 0 on the bottom level")
    max_turns: int | None = None
    max_tokens: int | None = None
    can_do_work: bool = False  # managers: also get hands-on tools
    review_tools: bool = True  # managers: may inspect the workspace / run public tests
    prompt: str = ""  # extra role instructions appended to the system prompt

    def output_cap(self) -> int:
        return self.max_tokens or DEFAULT_MAX_TOKENS[self.effort]


class OrgConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    levels: list[LevelSpec]
    communication: Literal["tree", "team_board", "global_board"] = "tree"
    memory: Literal["persistent", "fresh"] = "persistent"
    disclose_models: bool = False
    manager_max_turns: int = 12
    worker_max_turns: int = 24

    @model_validator(mode="after")
    def _check(self) -> "OrgConfig":
        if not self.levels:
            raise ValueError("an org needs at least one level")
        for i, lvl in enumerate(self.levels[:-1]):
            if lvl.span < 1:
                raise ValueError(f"level {i} ({lvl.role}) needs span >= 1")
        if self.levels[-1].span not in (0,):
            raise ValueError("the bottom level must have span 0")
        roles = [lvl.role for lvl in self.levels]
        if len(set(roles)) != len(roles):
            raise ValueError(f"level roles must be unique: {roles}")
        return self

    # ---- shape metrics ----
    @property
    def counts(self) -> list[int]:
        out, n = [], 1
        for lvl in self.levels:
            out.append(n)
            n *= max(lvl.span, 1)
        return out

    @property
    def depth(self) -> int:
        """Management layers above the workers (0 = a single agent)."""
        return len(self.levels) - 1

    @property
    def n_agents(self) -> int:
        return sum(self.counts)

    @property
    def n_workers(self) -> int:
        return self.counts[-1]

    @property
    def n_managers(self) -> int:
        return self.n_agents - self.n_workers

    @property
    def shape(self) -> str:
        return "-".join(str(c) for c in self.counts)

    def metrics(self) -> dict[str, Any]:
        spans = [lvl.span for lvl in self.levels[:-1]]
        return {
            "depth": self.depth,
            "levels": len(self.levels),
            "shape": self.shape,
            "n_agents": self.n_agents,
            "n_workers": self.n_workers,
            "n_managers": self.n_managers,
            "manager_ratio": round(self.n_managers / self.n_workers, 3),
            "avg_span": round((self.n_agents - 1) / self.n_managers, 3) if self.n_managers else 0,
            "max_span": max(spans) if spans else 0,
            "communication": self.communication,
        }

    def describe(self) -> str:
        parts = [f"{lvl.role} x{n} [{lvl.model}{'/' + lvl.effort if lvl.effort else ''}]"
                 for lvl, n in zip(self.levels, self.counts)]
        return " -> ".join(parts)

    def level_index(self, role: str) -> int:
        for i, lvl in enumerate(self.levels):
            if lvl.role == role:
                return i
        raise KeyError(f"org {self.name!r} has no role {role!r} (roles: {[l.role for l in self.levels]})")

    def with_overrides(
        self,
        models: dict[str, str] | None = None,
        efforts: dict[str, str | None] | None = None,
        sets: list[str] | None = None,
        name: str | None = None,
    ) -> "OrgConfig":
        """Return a copy with per-role model/effort swaps and dotted-path overrides.

        models={"worker": "qwen-flash"}; efforts={"ceo": "high"};
        sets=["levels.0.span=3", "communication=team_board"]. Role "top"/"bottom" also work.
        """
        data = self.model_dump()
        suffix = []
        for role, model in (models or {}).items():
            data["levels"][self._role_idx(role)]["model"] = model
            suffix.append(f"{role}={model}")
        for role, effort in (efforts or {}).items():
            data["levels"][self._role_idx(role)]["effort"] = effort
            suffix.append(f"{role}.effort={effort}")
        for item in sets or []:
            key, _, raw = item.partition("=")
            _set_path(data, key.strip(), yaml.safe_load(raw))
            suffix.append(item)
        if name:
            data["name"] = name
        elif suffix:
            data["name"] = f"{self.name}[{','.join(suffix)}]"
        return OrgConfig.model_validate(data)

    def _role_idx(self, role: str) -> int:
        if role == "top":
            return 0
        if role == "bottom":
            return len(self.levels) - 1
        return self.level_index(role)


def _set_path(data: Any, path: str, value: Any) -> None:
    keys = path.split(".")
    cur = data
    for k in keys[:-1]:
        cur = cur[int(k)] if isinstance(cur, list) else cur[k]
    last = keys[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def load_org(path: str | Path) -> OrgConfig:
    data = yaml.safe_load(Path(path).read_text())
    return OrgConfig.model_validate(data)


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


class LevelTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    effort: Effort | None = None
    role: str | None = None
    max_tokens: int | None = None
    max_turns: int | None = None
    prompt: str = ""


class GenerateSpec(BaseModel):
    """Generate a family of orgs with a fixed number of workers and varying depth.

    `ladder` lists management tiers from the top down (e.g. ceo, c-suite, director, manager).
    For depth d, the top tier is always kept and the remaining d-1 tiers are taken from the
    bottom of the ladder, so flattening removes middle management first. Set `ladder` to a
    single template to give every management tier the same model (structure-only control).
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    workers: int
    depths: list[int]
    ladder: list[LevelTemplate]
    worker: LevelTemplate
    spans: dict[int, list[int]] = {}  # optional explicit spans per depth
    communication: Literal["tree", "team_board", "global_board"] = "tree"
    include_solo: bool = False  # also emit a single-agent baseline using the top template

    def build(self) -> list[OrgConfig]:
        orgs = []
        if self.include_solo:
            top = self.ladder[0]
            orgs.append(OrgConfig(
                name=f"{self.name}-solo",
                levels=[LevelSpec(role=top.role or "solo", model=top.model, effort=top.effort,
                                  max_tokens=top.max_tokens, max_turns=top.max_turns, prompt=top.prompt)],
            ))
        for d in self.depths:
            spans = self.spans.get(d) or balanced_spans(self.workers, d)
            if len(self.ladder) == 1:
                tiers = [self.ladder[0]] * d
            elif d <= len(self.ladder):
                tiers = [self.ladder[0]] + self.ladder[len(self.ladder) - (d - 1):] if d > 1 else [self.ladder[0]]
            else:
                tiers = [self.ladder[0]] + [self.ladder[-1]] * (d - 1)
            levels = []
            for i, (tpl, span) in enumerate(zip(tiers, spans)):
                role = tpl.role or ("lead" if i == 0 else f"manager-l{i}")
                if any(l.role == role for l in levels):
                    role = f"{role}-l{i}"
                levels.append(LevelSpec(role=role, model=tpl.model, effort=tpl.effort, span=span,
                                        max_tokens=tpl.max_tokens, max_turns=tpl.max_turns, prompt=tpl.prompt))
            w = self.worker
            levels.append(LevelSpec(role=w.role or "worker", model=w.model, effort=w.effort,
                                    max_tokens=w.max_tokens, max_turns=w.max_turns, prompt=w.prompt))
            orgs.append(OrgConfig(
                name=f"{self.name}-d{d}",
                description=f"generated: {self.workers} workers, depth {d}",
                levels=levels,
                communication=self.communication,
            ))
        return orgs
