import pytest

from swarmy.catalog import Catalog
from swarmy.config import GenerateSpec, LevelSpec, OrgConfig, balanced_spans, load_org
from swarmy.paths import CONFIG_DIR


def test_example_orgs_load_and_resolve():
    catalog = Catalog.load(offline=True)
    files = sorted((CONFIG_DIR / "orgs").glob("*.yaml"))
    assert files
    for f in files:
        org = load_org(f)
        for node in org.nodes():
            catalog.resolve(node.settings.model)


def test_corporate_metrics():
    org = load_org(CONFIG_DIR / "orgs" / "field-army.yaml")
    m = org.metrics()
    assert m["shape"] == "1-2-4-8-16"
    assert m["n_agents"] == 31 and m["n_workers"] == 16 and m["depth"] == 4
    assert m["avg_span"] == 2.0


def test_overrides_swap_models_and_rename():
    org = load_org(CONFIG_DIR / "orgs" / "regiment.yaml")
    new = org.with_overrides({"private": "qwen-flash", "top": "opus"}, {"colonel": "high"}, ["levels.1.span=4"])
    assert new.levels[0].model == "opus" and new.levels[0].effort == "high"
    assert new.levels[2].model == "qwen-flash"
    assert new.n_workers == 8
    assert new.name.startswith("regiment[")
    with pytest.raises(KeyError):
        org.with_overrides({"nope": "haiku"})


def test_validation():
    with pytest.raises(ValueError):
        OrgConfig(name="x", levels=[LevelSpec(role="a", model="haiku", span=0), LevelSpec(role="b", model="haiku")])
    with pytest.raises(ValueError):
        OrgConfig(name="x", levels=[LevelSpec(role="a", model="haiku", span=2)])


@pytest.mark.parametrize("workers,depth,expected", [(8, 1, [8]), (8, 3, [2, 2, 2]), (8, 4, [1, 2, 2, 2]),
                                                    (12, 2, [3, 4]), (4, 3, [1, 2, 2]), (7, 2, [1, 7])])
def test_balanced_spans(workers, depth, expected):
    spans = balanced_spans(workers, depth)
    assert spans == expected
    prod = 1
    for s in spans:
        prod *= s
    assert prod == workers


def test_generate_removes_middle_tiers_first():
    spec = GenerateSpec(name="corp", workers=8, depths=[1, 2, 3, 4], include_solo=True,
                        ladder=[{"role": "ceo", "model": "opus"}, {"role": "c-suite", "model": "opus"},
                                {"role": "director", "model": "sonnet"}, {"role": "manager", "model": "haiku"}],
                        worker={"model": "gpt-oss-120b"})
    orgs = {o.name: o for o in spec.build()}
    assert [l.role for l in orgs["corp-solo"].levels] == ["ceo"]
    assert [l.role for l in orgs["corp-d1"].levels] == ["ceo", "private"]
    assert [l.role for l in orgs["corp-d2"].levels] == ["ceo", "manager", "private"]
    assert [l.role for l in orgs["corp-d3"].levels] == ["ceo", "director", "manager", "private"]
    assert [l.role for l in orgs["corp-d4"].levels] == ["ceo", "c-suite", "director", "manager", "private"]
    assert all(o.n_workers == 8 for n, o in orgs.items() if n != "corp-solo")


def test_catalog_shorthand_and_costs():
    from swarmy.providers.base import Usage

    cat = Catalog.load(offline=True)
    assert cat.resolve("anthropic:claude-sonnet-5-5").price.input == 2.0
    with pytest.raises(KeyError):
        cat.resolve("nonsense")
    haiku = cat.resolve("haiku")
    short = haiku.cost(Usage(input_tokens=10_000, output_tokens=1_000))
    long = haiku.cost(Usage(input_tokens=150_000, output_tokens=1_000))
    assert short == pytest.approx((10_000 * 0.10 + 1_000 * 0.50) / 1e6)
    assert long == pytest.approx((150_000 * 0.50 + 1_000 * 2.50) / 1e6)  # long-context tier
    assert cat.resolve("opus").map_effort("max") == "max"


def test_default_titles_are_military_ranks():
    from swarmy.ranks import management_ranks

    spec = GenerateSpec(name="g", workers=8, depths=[1, 2, 4], include_solo=True, ladder=[{"model": "sonnet"}],
                        worker={"model": "haiku"})
    orgs = {o.name: [lv.role for lv in o.levels] for o in spec.build()}
    assert orgs["g-solo"] == ["lone-wolf"]
    assert orgs["g-d1"] == ["field-marshal", "private"]
    assert orgs["g-d2"] == ["field-marshal", "captain", "private"]
    assert orgs["g-d4"] == ["field-marshal", "general", "colonel", "captain", "private"]
    assert len(management_ranks(15)) == 15


def test_auto_names_slug_spaces():
    org = OrgConfig.model_validate({"name": "x", "levels": [{"role": "lieutenant colonel", "model": "haiku", "span": 2},
                                                             {"role": "private", "model": "haiku"}]})
    assert [n.name for n in org.nodes()] == ["lieutenant-colonel", "private-1", "private-2"]
    assert org.is_uniform()
