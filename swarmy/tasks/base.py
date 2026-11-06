"""Task interface, document corpus with BM25 search, and the sandboxed case runner used for grading."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..sandbox import sandbox_env, set_limits


@dataclass
class Grade:
    score: float | None  # 0..1, or None for ungraded tasks (missions)
    passed: bool
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@dataclass
class Doc:
    doc_id: str
    title: str
    text: str


class Corpus:
    def __init__(self, docs: list[Doc]):
        self.docs = {d.doc_id: d for d in docs}
        self._by_title = {d.title.lower(): d for d in docs}
        self._tf = {d.doc_id: Counter(tokenize(d.title + " " + d.title + " " + d.text)) for d in docs}
        self._len = {k: sum(v.values()) for k, v in self._tf.items()}
        self._avg = sum(self._len.values()) / max(1, len(self._len))
        df: Counter = Counter()
        for tf in self._tf.values():
            df.update(tf.keys())
        n = len(docs)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, k: int = 6) -> list[tuple[Doc, float]]:
        terms = tokenize(query)
        scores = {}
        for doc_id, tf in self._tf.items():
            s = 0.0
            for t in terms:
                f = tf.get(t, 0)
                if f:
                    s += self._idf[t] * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * self._len[doc_id] / self._avg))
            if s > 0:
                scores[doc_id] = s
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        return [(self.docs[d], s) for d, s in ranked]

    def get(self, key: str) -> Doc | None:
        key = key.strip()
        return self.docs.get(key) or self._by_title.get(key.lower())

    def __len__(self) -> int:
        return len(self.docs)


# ---------------------------------------------------------------------------
# Task base
# ---------------------------------------------------------------------------

class TaskInstance:
    family: str = "base"

    def __init__(self, params: dict, seed: int):
        self.params = dict(params)
        self.seed = seed
        self.title = ""
        self.brief = ""
        self.corpus: Corpus | None = None
        self.workspace_files: dict[str, str] = {}
        self.answer_required = False
        self.key_terms: list[str] = []  # names that must reach the workers (coverage metric)

    @property
    def task_id(self) -> str:
        p = ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.family}/{p}/s{self.seed}" if p else f"{self.family}/s{self.seed}"

    @property
    def has_workspace(self) -> bool:
        return bool(self.workspace_files)

    @property
    def has_public_tests(self) -> bool:
        return False

    def setup_workspace(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        for rel, content in self.workspace_files.items():
            f = path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)

    def grade(self, answer: str | None, workspace: Path) -> Grade:
        raise NotImplementedError

    def progress(self, workspace: Path) -> float | None:
        """Cheap intermediate score of the workspace (None if not applicable)."""
        return None

    def public_tests(self, workspace: Path) -> str:
        return "This task has no tests."

    def gold(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Case runner: executes model-written code against JSON test cases in a subprocess.
# ---------------------------------------------------------------------------

RUNNER = r'''
import importlib, json, math, signal, sys
sys.path.insert(0, sys.argv[1])
cases = json.load(open(sys.argv[2]))
class CaseTimeout(BaseException):
    pass
def _alarm(signum, frame):
    raise CaseTimeout("timed out")
signal.signal(signal.SIGALRM, _alarm)
def same(a, b, tol):
    if tol and isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return math.isfinite(a) and abs(a - b) <= tol
    return a == b
results = []
for c in cases:
    try:
        signal.alarm(3)
        fn = getattr(importlib.import_module(c["module"]), c["func"])
        got = fn(*json.loads(json.dumps(c["args"])))
        signal.alarm(0)
        got = json.loads(json.dumps(got))
        ok = same(got, c["expected"], c.get("tol"))
        results.append({"ok": ok, "got": repr(got)[:160]})
    except BaseException as e:
        signal.alarm(0)
        results.append({"ok": False, "error": (type(e).__name__ + ": " + str(e))[:160]})
sys.stdout.write("\n__SWARMY_RESULT__" + json.dumps(results) + "\n")
'''


def run_cases(workspace: Path, cases: list[dict], timeout: float = 90) -> list[dict]:
    """Run cases against a *copy* of the workspace. Returns one result dict per case."""
    if not cases:
        return []
    with tempfile.TemporaryDirectory(prefix="swarmy-grade-") as tmp:
        tmp = Path(tmp)
        ws = tmp / "ws"
        shutil.copytree(workspace, ws, ignore=shutil.ignore_patterns("__pycache__", ".swarmy"))
        (tmp / "cases.json").write_text(json.dumps(cases))
        (tmp / "runner.py").write_text(RUNNER)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(tmp / "runner.py"), str(ws), str(tmp / "cases.json")],
                cwd=ws, capture_output=True, text=True, timeout=timeout,
                env=sandbox_env(ws, tmp), preexec_fn=set_limits(timeout),
            )
            out = proc.stdout
        except subprocess.TimeoutExpired:
            return [{"ok": False, "error": "grading timed out"} for _ in cases]
        marker = out.rfind("__SWARMY_RESULT__")
        if marker < 0:
            err = (proc.stderr or "no output")[-300:]
            return [{"ok": False, "error": f"runner failed: {err}"} for _ in cases]
        line = out[marker + len("__SWARMY_RESULT__"):].splitlines()[0]
        return json.loads(line)
