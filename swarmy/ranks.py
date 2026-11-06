"""Default position titles: ranks of the old French army (Ancien Régime / Napoleonic).

Titles are only labels: an agent's behaviour comes from its position in the tree, its model and
its tools, never from its rank. These are just the defaults the generator and the designer use.
"""

from __future__ import annotations

# Officer ranks, most senior first.
OFFICER_LADDER = [
    "connétable",
    "maréchal",
    "lieutenant-général",
    "maréchal-de-camp",
    "brigadier",
    "colonel",
    "major",
    "chef-de-bataillon",
    "capitaine",
    "lieutenant",
    "sergent",
    "caporal",
    "anspessade",
]
WORKER_RANK = "mousquetaire"
SOLO_RANK = "franc-tireur"

GLOSS = {
    "connétable": "Constable of France: supreme commander of the army",
    "maréchal": "Marshal of France: commands an army corps",
    "lieutenant-général": "lieutenant general: commands a division",
    "maréchal-de-camp": "camp marshal: a general officer under the lieutenant general",
    "brigadier": "brigadier: commands a brigade of regiments",
    "colonel": "colonel: commands a regiment",
    "major": "major: the regiment's second-in-command",
    "chef-de-bataillon": "battalion chief: commands a battalion of companies",
    "capitaine": "captain: commands a company",
    "lieutenant": "lieutenant: the captain's second, leads a section",
    "sergent": "sergeant: leads a squad",
    "caporal": "corporal: leads a small file of soldiers",
    "anspessade": "lance-corporal: the most junior leader",
    "mousquetaire": "musketeer: does the hands-on work",
    "franc-tireur": "irregular sharpshooter: works alone, no chain of command",
    "éclaireur": "scout: reconnaissance (research)",
    "sapeur": "sapper: field engineer (building and coding)",
}

# Which management ranks to use for an org with a given number of management layers,
# picked so the titles read naturally (a lone officer over musketeers is a capitaine).
_BY_DEPTH = {
    1: ["capitaine"],
    2: ["colonel", "capitaine"],
    3: ["maréchal", "colonel", "capitaine"],
    4: ["connétable", "maréchal", "colonel", "capitaine"],
    5: ["connétable", "maréchal", "lieutenant-général", "colonel", "capitaine"],
    6: ["connétable", "maréchal", "lieutenant-général", "colonel", "capitaine", "sergent"],
    7: ["connétable", "maréchal", "lieutenant-général", "brigadier", "colonel", "capitaine", "sergent"],
    8: ["connétable", "maréchal", "lieutenant-général", "maréchal-de-camp", "brigadier", "colonel", "capitaine",
        "sergent"],
}


def management_ranks(depth: int) -> list[str]:
    """Titles for `depth` management layers, top first."""
    if depth <= 0:
        return []
    if depth in _BY_DEPTH:
        return list(_BY_DEPTH[depth])
    extra = [f"officier-{i}" for i in range(1, max(0, depth - len(OFFICER_LADDER)) + 1)]
    return (OFFICER_LADDER + extra)[:depth]


def rank_between(upper: str | None, lower: str | None) -> str:
    """A title for a new layer inserted between two ranks (used by the designer): the rank just
    below `upper`, or just above `lower` when `upper` is not a known rank."""
    lad = OFFICER_LADDER
    if upper not in lad and lower not in lad:
        return "lieutenant"
    hi = lad.index(upper) if upper in lad else None
    lo = lad.index(lower) if lower in lad else len(lad)
    if hi is not None and hi + 1 < lo:
        return lad[hi + 1]
    if hi is None and lo > 0:
        return lad[lo - 1]
    return f"{upper or 'officier'}-adjoint"
