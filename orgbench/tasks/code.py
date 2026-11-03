"""Coding tasks: implement a procedurally generated suite of functions with hidden tests.

Each function is a precisely specified transformation drawn from a library of primitives
with randomized parameters (so specs are unique per seed). `coupling` controls how often a
function is defined in terms of an earlier one: 0 = fully independent (parallel-friendly),
1 = long dependency chains (errors propagate, coordination matters).
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .base import Grade, TaskInstance, run_cases

TYPE_NAMES = {"S": "str", "L": "list[int]", "I": "int"}


@dataclass
class Prim:
    name: str
    in_t: str
    out_t: str
    params: Callable[[random.Random], dict]
    spec: str
    body: str


def _p(**kw):
    return lambda rng: {k: (v(rng) if callable(v) else v) for k, v in kw.items()}


PRIMS: list[Prim] = [
    Prim("caesar_mixed", "S", "S", _p(k=lambda r: r.randint(1, 25), k2=lambda r: r.randint(1, 25)),
         "Shift each lowercase ASCII letter forward by {k} positions in the alphabet (wrapping from z to a) "
         "and each uppercase ASCII letter backward by {k2} positions (wrapping from A to Z). "
         "All other characters are unchanged.",
         """out = []
for ch in x:
    if 'a' <= ch <= 'z':
        out.append(chr((ord(ch) - 97 + {k}) % 26 + 97))
    elif 'A' <= ch <= 'Z':
        out.append(chr((ord(ch) - 65 - {k2}) % 26 + 65))
    else:
        out.append(ch)
return ''.join(out)"""),
    Prim("vowel_reverse", "S", "S", _p(ch=lambda r: r.choice("*#@%&")),
         "Replace every vowel (a, e, i, o, u in either case) with the character '{ch}', then reverse the whole string.",
         "return ''.join('{ch}' if c in 'aeiouAEIOU' else c for c in x)[::-1]"),
    Prim("rle", "S", "S", _p(),
         "Run-length encode the string: each maximal run of n copies of the same character c becomes the decimal "
         "number n followed by c (for example 'aaab!' becomes '3a1b1!'). The empty string maps to the empty string.",
         """out = []
i = 0
while i < len(x):
    j = i
    while j < len(x) and x[j] == x[i]:
        j += 1
    out.append(str(j - i) + x[i])
    i = j
return ''.join(out)"""),
    Prim("case_stride", "S", "S", _p(k=lambda r: r.randint(2, 4)),
         "Uppercase every character whose 0-based index is a multiple of {k} and lowercase every other character.",
         "return ''.join(c.upper() if i % {k} == 0 else c.lower() for i, c in enumerate(x))"),
    Prim("word_reverse", "S", "S", _p(sep=lambda r: r.choice(["-", "|", "+", "/"])),
         "Split the string on single spaces (exactly like Python's str.split(' ')), reverse the order of the "
         "pieces, and join them with '{sep}'.",
         "return '{sep}'.join(x.split(' ')[::-1])"),
    Prim("dedupe_chars", "S", "S", _p(),
         "Remove every character that already appeared earlier in the string (case-sensitive), keeping the first "
         "occurrence of each character in its original order.",
         """seen = set()
out = []
for c in x:
    if c not in seen:
        seen.add(c)
        out.append(c)
return ''.join(out)"""),
    Prim("weighted_letters", "S", "I", _p(m=lambda r: r.randint(2, 5)),
         "For each ASCII letter at 0-based index i, add (its position in the alphabet, case-insensitive, a=1 ... "
         "z=26) multiplied by (i mod {m} + 1). Non-letters add nothing but still occupy an index. Return the total.",
         """total = 0
for i, c in enumerate(x):
    if c.isascii() and c.isalpha():
        total += (ord(c.lower()) - 96) * (i % {m} + 1)
return total"""),
    Prim("count_pattern", "S", "I", _p(pat=lambda r: r.choice(["lo", "ka", "ve", "ar", "ana", "ese", "or"])),
         "Count the occurrences of the substring '{pat}' in the lowercased string, including overlapping occurrences.",
         """s = x.lower()
n = len('{pat}')
return sum(1 for i in range(len(s) - n + 1) if s[i:i + n] == '{pat}')"""),
    Prim("longest_run", "S", "I", _p(),
         "Return the length of the longest run of consecutive identical characters (0 for the empty string).",
         """best = 0
run = 0
prev = None
for c in x:
    run = run + 1 if c == prev else 1
    prev = c
    best = max(best, run)
return best"""),
    Prim("rotate", "L", "L", _p(k=lambda r: r.randint(1, 7)),
         "Rotate the list to the right by {k} positions; the shift is taken modulo the list length and an empty "
         "list stays empty. (A right rotation by 1 turns [1, 2, 3] into [3, 1, 2].)",
         """if not x:
    return []
k = {k} % len(x)
return x[-k:] + x[:-k] if k else list(x)"""),
    Prim("chunk_sums", "L", "L", _p(n=lambda r: r.randint(2, 4)),
         "Split the list into consecutive chunks of {n} elements (the last chunk may be shorter) and return the "
         "list of chunk sums.",
         "return [sum(x[i:i + {n}]) for i in range(0, len(x), {n})]"),
    Prim("running_max_gap", "L", "L", _p(),
         "Return a list of the same length whose element i equals max(x[0], ..., x[i]) - x[i].",
         """out = []
best = None
for v in x:
    best = v if best is None else max(best, v)
    out.append(best - v)
return out"""),
    Prim("scale_clamp", "L", "L",
         _p(a=lambda r: r.choice([2, 3, -2]), lo=lambda r: r.randint(-10, 0), hi=lambda r: r.randint(20, 60)),
         "Multiply each element by {a}, then clamp each result into the inclusive range [{lo}, {hi}].",
         "return [min({hi}, max({lo}, v * {a})) for v in x]"),
    Prim("unique_desc", "L", "L", _p(),
         "Remove duplicate values and return the remaining values sorted in descending order.",
         "return sorted(set(x), reverse=True)"),
    Prim("pair_mod", "L", "L", _p(m=lambda r: r.randint(5, 13)),
         "Return the list of (x[i] + x[i + 1]) % {m} for each adjacent pair, using Python's % semantics "
         "(results are never negative). A list with fewer than 2 elements gives [].",
         "return [(x[i] + x[i + 1]) % {m} for i in range(len(x) - 1)]"),
    Prim("alt_sum", "L", "I", _p(),
         "Return x[0] - x[1] + x[2] - x[3] + ... (alternating signs, starting with +). The empty list gives 0.",
         "return sum(v if i % 2 == 0 else -v for i, v in enumerate(x))"),
    Prim("inversions", "L", "I", _p(),
         "Count the pairs of positions (i, j) with i < j and x[i] > x[j].",
         "return sum(1 for i in range(len(x)) for j in range(i + 1, len(x)) if x[i] > x[j])"),
    Prim("kth_distinct", "L", "I", _p(k=lambda r: r.randint(2, 3)),
         "Return the {k}-th largest distinct value in the list (1-based: the 1st largest is the maximum). "
         "Return -1 if the list has fewer than {k} distinct values.",
         """vals = sorted(set(x), reverse=True)
return vals[{k} - 1] if len(vals) >= {k} else -1"""),
    Prim("digit_poly", "I", "I", _p(b=lambda r: r.randint(2, 7)),
         "Let d0, d1, ..., dn be the decimal digits of abs(x) from most significant to least significant. "
         "Return d0*{b}**0 + d1*{b}**1 + ... + dn*{b}**n.",
         """digits = [int(c) for c in str(abs(x))]
return sum(d * {b} ** i for i, d in enumerate(digits))"""),
    Prim("collatz_variant", "I", "I", _p(d=lambda r: r.choice([3, 5, 7])),
         "Start with n = abs(x), using n = 1 if x is 0. Repeat until n == 1: if n is divisible by {d}, set "
         "n = n // {d}; otherwise if n is even, set n = n // 2; otherwise set n = 3 * n + 1. Return the number of "
         "steps taken, but if 500 steps have been taken and n is still not 1, stop and return 500.",
         """n = abs(x) or 1
steps = 0
while n != 1:
    if steps == 500:
        return 500
    if n % {d} == 0:
        n //= {d}
    elif n % 2 == 0:
        n //= 2
    else:
        n = 3 * n + 1
    steps += 1
return steps"""),
    Prim("digital_root_base", "I", "I", _p(b=lambda r: r.randint(3, 9)),
         "Start with n = abs(x). While n >= {b}, replace n with the sum of its digits written in base {b}. Return n.",
         """n = abs(x)
while n >= {b}:
    s = 0
    while n:
        s += n % {b}
        n //= {b}
    n = s
return n"""),
    Prim("base_digits", "I", "L", _p(b=lambda r: r.randint(3, 9)),
         "Return the list of digits of abs(x) written in base {b}, most significant digit first ([0] for 0).",
         """n = abs(x)
if n == 0:
    return [0]
out = []
while n:
    out.append(n % {b})
    n //= {b}
return out[::-1]"""),
    Prim("codes_mod", "S", "L", _p(m=lambda r: r.randint(7, 19)),
         "Return the list [ord(c) % {m} for each character c of the string].",
         "return [ord(c) % {m} for c in x]"),
    Prim("letters", "L", "S", _p(off=lambda r: r.randint(0, 25)),
         "Map each element v to the lowercase letter chr(97 + (v + {off}) % 26), using Python's % semantics, and "
         "join the letters into one string.",
         "return ''.join(chr(97 + (v + {off}) % 26) for v in x)"),
]

SYL = ["ka", "lo", "ve", "ar", "an", "a", "es", "e", "or", "mi", "Zu", "BA", "Qx", "tt"]


def random_input(t: str, rng: random.Random):
    if t == "S":
        parts = []
        for _ in range(rng.randint(1, 8)):
            r = rng.random()
            if r < 0.6:
                parts.append(rng.choice(SYL) * rng.choice([1, 1, 1, 2]))
            elif r < 0.75:
                parts.append(" ")
            elif r < 0.9:
                parts.append(rng.choice(string.ascii_letters))
            else:
                parts.append(rng.choice("!?.,-019"))
        s = "".join(parts)
        return s if rng.random() > 0.1 else s.upper()
    if t == "L":
        return [rng.randint(-20, 60) for _ in range(rng.randint(1, 10))]
    return rng.randint(1, 9999)


EDGE = {"S": ["", "aaab  ZZ!"], "L": [[], [5, 5, -3, 12]], "I": [1, 0]}


@dataclass
class Func:
    name: str
    prim: Prim
    params: dict
    dep: "Func | None"

    @property
    def in_t(self) -> str:
        return self.dep.in_t if self.dep else self.prim.in_t

    @property
    def out_t(self) -> str:
        return self.prim.out_t

    def body(self) -> str:
        lines = self.prim.body.format(**self.params).splitlines()
        if self.dep:
            lines = [f"x = {self.dep.name}(x)"] + lines
        return "\n".join("    " + ln for ln in lines)

    def source(self, with_imports: bool = True) -> str:
        head = f"from funcs.{self.dep.name} import {self.dep.name}\n\n\n" if (self.dep and with_imports) else ""
        return f"{head}def {self.name}(x):\n{self.body()}\n"

    def stub(self) -> str:
        head = f"from funcs.{self.dep.name} import {self.dep.name}\n\n\n" if self.dep else ""
        return (f"# {self.name}: see SPEC.md for the exact specification.\n{head}"
                f"def {self.name}(x):\n    raise NotImplementedError\n")

    def spec_text(self) -> str:
        prim_spec = self.prim.spec.format(**self.params)
        sig = f"`{self.name}(x: {TYPE_NAMES[self.in_t]}) -> {TYPE_NAMES[self.out_t]}`"
        if self.dep:
            return (f"{sig}  (module `funcs/{self.name}.py`)\n\nFirst compute y = {self.dep.name}(x) by calling "
                    f"`{self.dep.name}` from this suite. Then apply the following to y and return the result: {prim_spec}")
        return f"{sig}  (module `funcs/{self.name}.py`)\n\n{prim_spec}"


class CodeTask(TaskInstance):
    family = "code"

    def __init__(self, params: dict, seed: int):
        super().__init__(params, seed)
        n = int(params.get("n", 6))
        coupling = float(params.get("coupling", 0.3))
        rng = random.Random(f"code-{seed}-{n}-{coupling}")
        funcs: list[Func] = []
        pool = PRIMS[:]
        rng.shuffle(pool)
        for j in range(n):
            name = f"step_{j + 1:02d}"
            dep = None
            if j > 0 and rng.random() < coupling:
                dep = rng.choice(funcs)
                options = [p for p in PRIMS if p.in_t == dep.out_t]
                prim = rng.choice(options)
            else:
                prim = pool.pop() if pool else rng.choice(PRIMS)
            funcs.append(Func(name, prim, prim.params(rng), dep))
        self.funcs = funcs
        self._ref = self._compile_reference()
        self.public_cases = self._cases(rng, per_func=2, edge=False)
        self.hidden_cases = self._cases(random.Random(f"hidden-{seed}-{n}-{coupling}"), per_func=10, edge=True)
        self.title = f"Implement a {n}-function Python suite"
        self.key_terms = [f.name for f in funcs]
        self.workspace_files = self._workspace()
        deps = sum(1 for f in funcs if f.dep)
        self.brief = (
            f"Implement the {n} Python functions specified in SPEC.md in the shared workspace. Each function lives "
            f"in its own module under funcs/ (stubs are provided). {deps} of the functions are defined in terms of "
            f"other functions in the suite, as described in the spec.\n\n"
            f"Grading: hidden tests call every function with many inputs; the score is the fraction of hidden "
            f"tests passed. SPEC.md includes public examples, and the `run_tests` tool checks them.\n\n"
            f"Rules: keep module paths and function names exactly as given, use only the Python standard library, "
            f"and do not edit SPEC.md.\n\n"
            f"Hands-on roles can read and write files (read_file, write_file, list_files) and execute Python "
            f"(run_python). The full specification follows (it is also in SPEC.md):\n\n{self._spec_md()}"
        )

    # ---- reference ----
    def _compile_reference(self) -> dict:
        ns: dict = {}
        for f in self.funcs:
            exec(f.source(with_imports=False), ns)  # trusted: generated from our own templates
        return ns

    def reference_files(self) -> dict[str, str]:
        files = {"funcs/__init__.py": ""}
        for f in self.funcs:
            files[f"funcs/{f.name}.py"] = f.source()
        return files

    def _cases(self, rng: random.Random, per_func: int, edge: bool) -> list[dict]:
        cases = []
        for f in self.funcs:
            inputs = list(EDGE[f.in_t]) if edge else []
            while len(inputs) < per_func:
                inputs.append(random_input(f.in_t, rng))
            for x in inputs:
                cases.append({"module": f"funcs.{f.name}", "func": f.name, "args": [x],
                              "expected": self._ref[f.name](x)})
        return cases

    # ---- workspace ----
    def _spec_md(self) -> str:
        parts = ["# Function suite specification\n"]
        for f in self.funcs:
            parts.append(f"## {f.name}\n\n{f.spec_text()}\n")
            ex = [c for c in self.public_cases if c["func"] == f.name]
            if ex:
                parts.append("Examples:\n" + "\n".join(f"- `{f.name}({c['args'][0]!r})` -> `{c['expected']!r}`" for c in ex) + "\n")
        return "\n".join(parts)

    def _workspace(self) -> dict[str, str]:
        files = {"SPEC.md": self._spec_md(), "funcs/__init__.py": ""}
        for f in self.funcs:
            files[f"funcs/{f.name}.py"] = f.stub()
        return files

    @property
    def has_public_tests(self) -> bool:
        return True

    def public_tests(self, workspace: Path) -> str:
        return summarize_cases(self.public_cases, run_cases(workspace, self.public_cases))

    def _score(self, workspace: Path) -> tuple[float, dict]:
        results = run_cases(workspace, self.hidden_cases)
        per: dict[str, list[bool]] = {}
        for c, r in zip(self.hidden_cases, results):
            per.setdefault(c["func"], []).append(bool(r.get("ok")))
        frac = sum(sum(v) for v in per.values()) / max(1, len(self.hidden_cases))
        return frac, {k: f"{sum(v)}/{len(v)}" for k, v in per.items()}

    def grade(self, answer: str | None, workspace: Path) -> Grade:
        frac, per = self._score(workspace)
        return Grade(score=frac, passed=frac == 1.0, details={"per_function": per})

    def progress(self, workspace: Path) -> float | None:
        return self._score(workspace)[0]

    def gold(self) -> dict:
        return {"functions": {f.name: f.prim.name + (f" <- {f.dep.name}" if f.dep else "") for f in self.funcs}}


def summarize_cases(cases: list[dict], results: list[dict]) -> str:
    lines, passed = [], 0
    for c, r in zip(cases, results):
        call = f"{c['func']}({', '.join(repr(a) for a in c['args'])})"
        if r.get("ok"):
            passed += 1
            lines.append(f"PASS {call}")
        else:
            got = r.get("got") or r.get("error")
            lines.append(f"FAIL {call}: expected {c['expected']!r}, got {got}")
    return f"{passed}/{len(cases)} public tests passed\n" + "\n".join(lines)
