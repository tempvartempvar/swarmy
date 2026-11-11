"""Default position titles: military ranks, with a Field Marshal at the top.

Titles are only labels: an agent's behaviour comes from its position in the tree, its model, its
tools and its goal, never from its rank. These are just the defaults the generator and the
designer use; give positions any title you like.
"""

from __future__ import annotations

# Officer ranks, most senior first.
OFFICER_LADDER = [
    "field-marshal",
    "general",
    "lieutenant-general",
    "major-general",
    "brigadier",
    "colonel",
    "lieutenant-colonel",
    "major",
    "captain",
    "lieutenant",
    "sergeant",
    "corporal",
    "lance-corporal",
]
WORKER_RANK = "private"
SOLO_RANK = "lone-wolf"

GLOSS = {
    "field-marshal": "the supreme commander",
    "general": "commands an army",
    "lieutenant-general": "commands a corps",
    "major-general": "commands a division",
    "brigadier": "commands a brigade",
    "colonel": "commands a regiment",
    "lieutenant-colonel": "commands a battalion",
    "major": "second-in-command of a battalion",
    "captain": "commands a company",
    "lieutenant": "leads a platoon",
    "sergeant": "leads a section",
    "corporal": "leads a fire team",
    "lance-corporal": "the most junior leader",
    "private": "does the hands-on work",
    "lone-wolf": "works alone, no chain of command",
    "scout": "reconnaissance (research)",
    "sapper": "combat engineer (building and coding)",
}

# Ranks for an org with a given number of command layers. The top is always the Field Marshal.
_BY_DEPTH = {
    1: ["field-marshal"],
    2: ["field-marshal", "captain"],
    3: ["field-marshal", "colonel", "captain"],
    4: ["field-marshal", "general", "colonel", "captain"],
    5: ["field-marshal", "general", "major-general", "colonel", "captain"],
    6: ["field-marshal", "general", "major-general", "colonel", "captain", "sergeant"],
    7: ["field-marshal", "general", "major-general", "brigadier", "colonel", "captain", "sergeant"],
    8: ["field-marshal", "general", "lieutenant-general", "major-general", "brigadier", "colonel", "captain",
        "sergeant"],
}


def management_ranks(depth: int) -> list[str]:
    """Titles for `depth` command layers, top first."""
    if depth <= 0:
        return []
    if depth in _BY_DEPTH:
        return list(_BY_DEPTH[depth])
    extra = [f"officer-{i}" for i in range(1, max(0, depth - len(OFFICER_LADDER)) + 1)]
    return (OFFICER_LADDER + extra)[:depth]

