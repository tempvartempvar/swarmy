from pathlib import Path

import pytest

from orgbench.tasks import make_task, parse_task_spec
from orgbench.tasks.qa import match_answer


@pytest.mark.parametrize("family,params", [
    ("code", {"n": 8, "coupling": 0.0}), ("code", {"n": 8, "coupling": 1.0}),
    ("mixed", {}), ("mixed", {"rules": 1}), ("mixed", {"rules": 3, "products": 7, "cities": 6}),
])
@pytest.mark.parametrize("seed", [1, 7])
def test_reference_solution_scores_one_and_stub_scores_zero(tmp_path, family, params, seed):
    task = make_task(family, params, seed)
    ws = tmp_path / "ws"
    task.setup_workspace(ws)
    assert task.grade(None, ws).score == 0.0
    n_public = len(task.public_cases)
    assert f"0/{n_public} public tests passed" in task.public_tests(ws)
    for rel, src in task.reference_files().items():
        (ws / rel).write_text(src)
    g = task.grade(None, ws)
    assert g.score == 1.0 and g.passed, g.details
    assert f"{n_public}/{n_public} public tests passed" in task.public_tests(ws)


def test_code_partial_credit(tmp_path):
    task = make_task("code", {"n": 6, "coupling": 0.0}, 3)
    ws = tmp_path / "ws"
    task.setup_workspace(ws)
    ref = task.reference_files()
    first = sorted(k for k in ref if k.startswith("funcs/step"))[0]
    (ws / first).write_text(ref[first])
    assert task.grade(None, ws).score == pytest.approx(1 / 6)


def test_hostile_code_does_not_break_grader(tmp_path):
    task = make_task("code", {"n": 3, "coupling": 0.0}, 1)
    ws = tmp_path / "ws"
    task.setup_workspace(ws)
    (ws / "funcs" / "step_01.py").write_text("while True:\n    pass\n")
    (ws / "funcs" / "step_02.py").write_text("import sys\nsys.exit(3)\n")
    g = task.grade(None, ws)
    assert g.score == 0.0


@pytest.mark.parametrize("params", [{"kind": "chain", "hops": h} for h in (2, 3, 4)] +
                         [{"kind": "aggregate", "n": 5}, {"kind": "census"}])
def test_qa_gold_answers_are_findable(params):
    task = make_task("qa", params, 5)
    expected = task.gold()["expected"]
    assert task.grade(str(expected), Path(".")).passed
    assert not task.grade("I don't know", Path(".")).passed
    assert task.key_terms


def test_qa_documents_contain_the_facts():
    task = make_task("qa", {"kind": "chain", "hops": 3}, 2)
    w = task.world
    product = task.key_terms[0]
    maker = w.products[product].maker
    city = w.companies[maker].hq
    texts = " ".join(d.text for d in task.corpus.docs.values())
    assert f"manufactured by {maker}" in texts
    assert f"headquartered in {city}" in texts
    assert f"{w.cities[city].population:,}" in texts
    top = [d.title for d, _ in task.corpus.search(product, k=3)]
    assert product in top


def test_match_answer():
    assert match_answer("1,204,000", 1204000)
    assert match_answer("The answer is 1204000.", 1204000)
    assert not match_answer("1204000 or 99", 1204000)
    assert match_answer("Velmora", "Velmora")
    assert match_answer("It is Velmora.", "Velmora", ["Velmora", "Kalo"])
    assert not match_answer("Velmora or Kalo", "Velmora", ["Velmora", "Kalo"])
    assert not match_answer("Velmoran", "Velmora", ["Velmora"])


def test_parse_task_spec():
    assert parse_task_spec("qa:kind=chain,hops=3") == ("qa", {"kind": "chain", "hops": 3})
    assert parse_task_spec("mixed") == ("mixed", {})


def test_tasks_are_deterministic():
    a, b = make_task("mixed", {}, 4), make_task("mixed", {}, 4)
    assert a.brief == b.brief and a.hidden_cases == b.hidden_cases
    assert make_task("mixed", {}, 5).brief != a.brief
