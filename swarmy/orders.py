"""The chain of orders for a run: the mission at the top, every order each commander issued
(linked to the order it was working on), and the report that came back up for each."""

from __future__ import annotations

import json

from .tracing import Tracer


def load_orders(tracer: Tracer, run_id: str, org_json: str | None = None) -> dict:
    evs = tracer.query("SELECT node, level, kind, data_json, ts FROM events WHERE run_id=? AND kind IN "
                       "('mission', 'assign', 'report') ORDER BY id", (run_id,))
    run = tracer.query("SELECT org_json, task_id, status, score FROM runs WHERE run_id=?", (run_id,))
    t0 = evs[0]["ts"] if evs else 0.0
    orders: dict[int, dict] = {}
    for e in evs:
        d = json.loads(e["data_json"] or "{}")
        oid = d.get("order_id")
        if oid is None:
            continue
        t = round(e["ts"] - t0, 2)
        if e["kind"] == "mission":
            orders[oid] = {"id": oid, "parent": None, "from": "client", "from_role": "client", "to": d.get("to"),
                           "to_role": d.get("to_role"), "level": 0, "title": d.get("title"), "text": d.get("text", ""),
                           "round": 1, "t": t, "report": None}
        elif e["kind"] == "assign":
            orders[oid] = {"id": oid, "parent": d.get("parent_order"), "from": e["node"],
                           "from_role": d.get("from_role"), "to": d.get("to"), "to_role": d.get("to_role"),
                           "level": d.get("to_level"), "text": d.get("text", d.get("preview", "")),
                           "fields": d.get("fields"), "round": d.get("round", 1), "t": t, "report": None}
        elif e["kind"] == "report" and oid in orders:
            orders[oid]["report"] = {"status": d.get("status"), "text": d.get("text", d.get("preview", "")),
                                     "answer": d.get("answer"), "t": t}
    goals = {}
    org_json = org_json or (run[0]["org_json"] if run else None)
    if org_json:
        try:
            from .config import OrgConfig

            org = OrgConfig.model_validate_json(org_json)
            goals = {n.name: n.settings.goal for n in org.nodes() if n.settings.goal}
        except Exception:
            pass
    meta = run[0] if run else {}
    mock = tracer.query("SELECT mock FROM runs WHERE run_id=?", (run_id,))
    return {"run_id": run_id, "orders": sorted(orders.values(), key=lambda o: o["id"]), "goals": goals,
            "task_id": meta.get("task_id"), "status": meta.get("status") or "running", "score": meta.get("score"),
            "mock": bool(mock and mock[0]["mock"])}


def render(data: dict, full: bool = False, width: int = 110) -> str:
    """Indented outline of the chain of orders (for the terminal)."""
    kids: dict = {}
    for o in data["orders"]:
        kids.setdefault(o["parent"], []).append(o)
    lines = []

    def clip(text: str, n: int) -> str:
        one = " ".join((text or "").split())
        return one if full or len(one) <= n else one[: n - 1] + "…"

    def walk(o: dict, depth: int) -> None:
        pad = "  " * depth
        status = o["report"]["status"] if o["report"] else "working…"
        rework = f" (round {o['round']})" if o.get("round", 1) > 1 else ""
        head = f"{pad}[{o['id']}] {o['from']} → {o['to']}{rework}  [{status}]"
        lines.append(head)
        objective = (o.get("fields") or {}).get("objective")
        lines.append(f"{pad}    order:  {clip(objective or o['text'], width - len(pad))}")
        goal = data["goals"].get(o["to"])
        if goal:
            lines.append(f"{pad}    goal:   {clip(goal, width - len(pad))}")
        if o["report"]:
            lines.append(f"{pad}    report: {clip(o['report']['text'], width - len(pad))}")
        for c in kids.get(o["id"], []):
            walk(c, depth + 1)

    for root in kids.get(None, []):
        walk(root, 0)
    return "\n".join(lines) if lines else "(no orders recorded)"
