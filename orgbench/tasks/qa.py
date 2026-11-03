"""Research/QA tasks over the generated knowledge base.

kind=chain      sequential multi-hop lookup (hops=2..4). Hard to parallelize.
kind=aggregate  n independent 2-hop lookups + argmax. Embarrassingly parallel.
kind=census     scan-and-count across many documents. Parallel by partition.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

from .base import Grade, TaskInstance
from .world import generate_world

ANSWER_RULES = (
    "Submit only the final answer in the `answer` field (a name or a number, no explanation). "
    "All facts must come from the knowledge base; do not guess."
)


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[\$,]", "", text)
    text = re.sub(r"[^a-z0-9. ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().rstrip(".")


def match_answer(answer: str | None, expected: str | int, candidates: list[str] | None = None) -> bool:
    if answer is None:
        return False
    a = normalize(str(answer))
    if isinstance(expected, int):
        nums = re.findall(r"-?\d+(?:\.\d+)?", a)
        return len(nums) >= 1 and float(nums[0]) == float(expected) and len(set(nums)) == 1
    e = normalize(expected)
    if a == e:
        return True
    if _contains(a, e) and len(a.split()) <= len(e.split()) + 6:
        # reject answers that also name another candidate (hedging)
        others = [normalize(c) for c in (candidates or []) if normalize(c) != e]
        return not any(_contains(a, o) for o in others)
    return False


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


class QATask(TaskInstance):
    family = "qa"

    def __init__(self, params: dict, seed: int):
        super().__init__(params, seed)
        kind = params.get("kind", "chain")
        world = generate_world(seed, scale=float(params.get("scale", 1.0)))
        self.world = world
        self.corpus = world.corpus
        self.answer_required = True
        rng = random.Random(f"qa-{kind}-{seed}-{sorted(params.items())}")
        if kind == "chain":
            self._chain(rng, int(params.get("hops", 3)))
        elif kind == "aggregate":
            self._aggregate(rng, int(params.get("n", 6)))
        elif kind == "census":
            self._census(rng)
        else:
            raise ValueError(f"unknown qa kind {kind!r}")
        self.title = f"Research question ({kind})"
        self.brief = (
            f"{self.question}\n\n"
            f"Resources: a knowledge base of {len(self.corpus)} short documents about fictional countries, "
            f"cities, companies, people and products. Hands-on roles can search it (search_docs) and read "
            f"documents (read_doc). Facts are spread across documents, and some documents are distractors.\n\n"
            f"{ANSWER_RULES}"
        )

    def _chain(self, rng: random.Random, hops: int) -> None:
        w = self.world
        product = rng.choice(sorted(w.products))
        maker = w.companies[w.products[product].maker]
        city = w.cities[maker.hq]
        if hops <= 2:
            self.question = f"In which city is the company that manufactures the {product} headquartered?"
            self.expected, self.candidates = city.name, list(w.cities)
        elif hops == 3:
            self.question = (f"What is the population of the city where the manufacturer of the "
                             f"{product} is headquartered?")
            self.expected, self.candidates = city.population, None
        else:
            ceo = w.people[maker.ceo]
            self.question = (f"In what year was the city founded where the Chief Executive Officer of the "
                             f"company that manufactures the {product} was born?")
            self.expected, self.candidates = w.cities[ceo.hometown].founded, None
        self.key_terms = [product]

    def _aggregate(self, rng: random.Random, n: int) -> None:
        w = self.world
        products = sorted(w.products)
        for _ in range(500):
            pick = rng.sample(products, k=min(n, len(products)))
            pops = [w.maker_city(p).population for p in pick]
            best = max(pops)
            if pops.count(best) == 1:
                break
        winner = pick[pops.index(best)]
        listed = ", ".join(pick)
        self.question = (
            f"Consider these products: {listed}. For each one, find the city where its manufacturer is "
            f"headquartered and that city's population. Which product's manufacturer is headquartered in "
            f"the most populous city? Answer with the product name."
        )
        self.expected, self.candidates = winner, pick
        self.key_terms = pick

    def _census(self, rng: random.Random) -> None:
        w = self.world
        options = []
        for city in w.cities:
            companies = [c.name for c in w.companies.values() if c.hq == city]
            if len(companies) < 2:
                continue
            for title in sorted({p.title for p in w.people.values()} - {"Chief Executive Officer"}):
                count = sum(1 for p in w.people.values() if p.title == title and p.employer in companies)
                if count >= 1:
                    options.append((city, title, count, companies))
        city, title, count, companies = rng.choice(sorted(options))
        self.question = (f"How many people in the knowledge base work as {title} at companies "
                         f"headquartered in {city}?")
        self.expected, self.candidates = count, None
        self.key_terms = [city, title]

    def grade(self, answer: str | None, workspace: Path) -> Grade:
        ok = match_answer(answer, self.expected, self.candidates)
        return Grade(score=1.0 if ok else 0.0, passed=ok, details={"expected": self.expected, "answer": answer})

    def gold(self) -> dict:
        return {"expected": self.expected}
