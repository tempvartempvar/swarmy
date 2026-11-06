"""Task families and spec parsing.

A task spec is `family:key=value,key=value`, e.g. `qa:kind=chain,hops=3` or `code:n=8,coupling=0.5`.
Free-form missions are `make_task("mission", {"text": "..."})` (CLI: `swarmy run --mission "..."`).
"""

from __future__ import annotations

import yaml

from .base import Grade, TaskInstance
from .code import CodeTask
from .greenhouse import GreenhouseTask
from .mission import MissionTask
from .mixed import MixedTask
from .qa import QATask

__all__ = ["FAMILIES", "Grade", "TaskInstance", "make_task", "parse_task_spec"]

FAMILIES = {"qa": QATask, "code": CodeTask, "mixed": MixedTask, "mission": MissionTask, "greenhouse": GreenhouseTask}


def make_task(family: str, params: dict | None = None, seed: int = 1) -> TaskInstance:
    if family not in FAMILIES:
        raise KeyError(f"unknown task family {family!r} (have: {', '.join(FAMILIES)})")
    return FAMILIES[family](params or {}, seed)


def parse_task_spec(spec: str) -> tuple[str, dict]:
    family, _, rest = spec.partition(":")
    params = {}
    for item in filter(None, (s.strip() for s in rest.split(","))):
        k, _, v = item.partition("=")
        params[k.strip()] = yaml.safe_load(v)
    return family.strip(), params
