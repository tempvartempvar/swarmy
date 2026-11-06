"""SQLite trace store: sweeps, runs, every LLM call, and organizational events."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sweeps (
    sweep_id TEXT PRIMARY KEY, name TEXT, config_json TEXT, budget_usd REAL,
    spent_usd REAL, mock INTEGER, started_at REAL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, sweep_id TEXT, org_name TEXT, org_json TEXT, shape TEXT,
    depth INTEGER, n_agents INTEGER, n_workers INTEGER, communication TEXT,
    task_id TEXT, family TEXT, task_params TEXT, seed INTEGER, rep INTEGER,
    status TEXT, score REAL, passed INTEGER, answer TEXT, grade_json TEXT,
    cost_usd REAL, budget_cap REAL, n_calls INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
    wall_s REAL, llm_s REAL, rate_wait_s REAL, error TEXT, mock INTEGER,
    started_at REAL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, node TEXT, level INTEGER, role TEXT,
    is_manager INTEGER, model_alias TEXT, model_id TEXT, served_model TEXT, provider TEXT, turn INTEGER,
    purpose TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    reasoning_tokens INTEGER, cost_usd REAL, latency_s REAL, rate_wait_s REAL, retries INTEGER,
    max_tokens INTEGER, stop_reason TEXT, n_tool_calls INTEGER, fallback INTEGER, ts REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, node TEXT, level INTEGER, kind TEXT,
    data_json TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS calls_run ON calls(run_id);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS runs_sweep ON runs(sweep_id);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:6]}"


class Tracer:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(calls)")}
        if "purpose" not in cols:  # databases created by older versions
            self.conn.execute("ALTER TABLE calls ADD COLUMN purpose TEXT")
            self.conn.commit()
        self._lock = threading.Lock()

    def _exec(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self.conn.execute(sql, args)
            self.conn.commit()

    def _insert(self, table: str, row: dict) -> None:
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        self._exec(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({qs})", tuple(row.values()))

    # ---- sweeps ----
    def start_sweep(self, sweep_id: str, name: str, config: dict, budget: float, mock: bool) -> None:
        self._insert("sweeps", {"sweep_id": sweep_id, "name": name, "config_json": json.dumps(config, default=str),
                                "budget_usd": budget, "spent_usd": 0.0, "mock": int(mock),
                                "started_at": time.time(), "finished_at": None})

    def finish_sweep(self, sweep_id: str, spent: float) -> None:
        self._exec("UPDATE sweeps SET spent_usd=?, finished_at=? WHERE sweep_id=?", (spent, time.time(), sweep_id))

    # ---- runs ----
    def save_run(self, row: dict) -> None:
        self._insert("runs", row)

    def log_call(self, row: dict) -> None:
        row.setdefault("ts", time.time())
        self._insert("calls", row)

    def log_event(self, run_id: str, node: str, level: int, kind: str, data: dict | None = None) -> None:
        self._insert("events", {"run_id": run_id, "node": node, "level": level, "kind": kind,
                                "data_json": json.dumps(data or {}, default=str), "ts": time.time()})

    # ---- queries ----
    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def resolve_sweep(self, ref: str | None) -> dict | None:
        if not ref or ref == "latest":
            rows = self.query("SELECT * FROM sweeps ORDER BY started_at DESC LIMIT 1")
        else:
            rows = self.query("SELECT * FROM sweeps WHERE sweep_id=? OR name=? ORDER BY started_at DESC LIMIT 1",
                              (ref, ref))
        return rows[0] if rows else None

    def close(self) -> None:
        self.conn.close()


class Transcript:
    """Per-run JSONL transcript for debugging (what each agent said and did)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, **record) -> None:
        record.setdefault("ts", round(time.time(), 3))
        with self._lock:
            self.f.write(json.dumps(record, default=str) + "\n")
            self.f.flush()

    def close(self) -> None:
        self.f.close()
