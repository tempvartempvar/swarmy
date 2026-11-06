"""Free-form missions: you give the top of the org an objective in plain words and watch how it
plans and hands out the work. There is a shared (initially empty) workspace for the results.
Missions are not graded automatically; inspect what was built in the run explorer or the run's
workspace folder."""

from __future__ import annotations

import re
from pathlib import Path

from .base import Grade, TaskInstance

ENVIRONMENT = (
    "Resources: a shared file workspace that starts empty. Hands-on roles can write and read files and run "
    "Python 3 (the standard library plus numpy and matplotlib). Put everything you build in the workspace; "
    "that is what the client receives. When you finish, report what was built, where it is, how to run it, "
    "and what is left undone."
)


class MissionTask(TaskInstance):
    family = "mission"

    def __init__(self, params: dict, seed: int):
        super().__init__(params, seed)
        text = str(params.get("text") or "").strip()
        if not text:
            raise ValueError("a mission needs text, e.g. make_task('mission', {'text': 'Build ...'})")
        self.text = text
        first = text.splitlines()[0]
        self.title = first if len(first) <= 80 else first[:77] + "..."
        self.brief = f"{text}\n\n{ENVIRONMENT}"
        self.answer_required = False
        self.key_terms = []

    @property
    def task_id(self) -> str:
        slug = "-".join(re.findall(r"[a-z0-9]+", self.text.lower())[:6]) or "mission"
        return f"mission/{slug}/s{self.seed}"

    @property
    def has_workspace(self) -> bool:
        return True

    def setup_workspace(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def grade(self, answer: str | None, workspace: Path) -> Grade:
        files = [p for p in workspace.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        return Grade(score=None, passed=False, details={"graded": False, "files": len(files),
                                                       "bytes": sum(p.stat().st_size for p in files)})

    def gold(self) -> dict:
        return {"mission": self.text}
