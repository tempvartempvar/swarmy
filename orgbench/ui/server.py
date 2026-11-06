"""Local web UI: an interactive org chart editor with live test runs.

Stdlib-only HTTP server. The browser holds the org being edited (as an explicit tree) and asks
`/api/analyze` for everything derived from it - names, metrics, contacts, wiki membership - so the
chart always shows exactly the rules the engine will run.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml
from pydantic import ValidationError

from ..catalog import Catalog
from ..config import GenerateSpec, OrgConfig, load_org
from ..paths import CONFIG_DIR, DEFAULT_RESULTS_DIR
from ..rules import KIND_LABELS, all_contacts, wiki_instances

STATIC = Path(__file__).parent / "static"
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
# Rough output tokens per call by effort, for the cost hint only.
TYPICAL_OUTPUT = {None: 1500, "low": 700, "medium": 1500, "high": 3000, "xhigh": 5000, "max": 8000}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

TASK_FAMILIES = [
    {"family": "qa", "label": "Research question",
     "presets": [{"label": "Multi-hop chain (sequential)", "spec": "qa:kind=chain,hops=3"},
                 {"label": "Aggregate over 6 items (parallel)", "spec": "qa:kind=aggregate,n=6"},
                 {"label": "Census count (scan)", "spec": "qa:kind=census"}]},
    {"family": "code", "label": "Coding",
     "presets": [{"label": "6 functions, some dependencies", "spec": "code:n=6,coupling=0.3"},
                 {"label": "8 independent functions", "spec": "code:n=8,coupling=0"},
                 {"label": "6 functions, chained", "spec": "code:n=6,coupling=1"}]},
    {"family": "mixed", "label": "Research + coding",
     "presets": [{"label": "Pricing from policy docs", "spec": "mixed:products=4,cities=3"},
                 {"label": "Pricing, larger scope", "spec": "mixed:products=6,cities=4"}]},
]


def _errors(e: Exception) -> str:
    if isinstance(e, ValidationError):
        parts = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"] if x not in ("function-after[_check(), OrgConfig]",))
            parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
        return "; ".join(parts)
    return str(e)


class UIState:
    def __init__(self, orgs_dir: Path, results_dir: Path, catalog: Catalog):
        self.orgs_dir = orgs_dir
        self.results_dir = results_dir
        self.catalog = catalog
        self.runs: dict[str, dict] = {}
        self.lock = threading.Lock()

    # ---- catalog ----
    def catalog_json(self) -> dict:
        models = []
        for alias, spec in self.catalog.models.items():
            models.append({"alias": alias, "provider": spec.provider, "model": spec.model,
                           "input": spec.price.input, "output": spec.price.output,
                           "free": spec.price.input == 0 and spec.price.output == 0 and spec.provider != "mock",
                           "rpm": spec.rpm})
        return {"models": models, "efforts": EFFORT_LEVELS, "kinds": KIND_LABELS,
                "keys": {"anthropic": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
                         "openrouter": bool(os.environ.get("OPENROUTER_API_KEY"))},
                "tasks": TASK_FAMILIES}

    # ---- orgs ----
    def list_orgs(self) -> list[dict]:
        out = []
        for f in sorted(self.orgs_dir.glob("*.yaml")):
            try:
                org = load_org(f)
                out.append({"file": f.stem, "name": org.name, "description": org.description,
                            "shape": org.shape, "n_agents": org.n_agents})
            except Exception as e:
                out.append({"file": f.stem, "name": f.stem, "error": _errors(e)})
        return out

    def load(self, file: str) -> dict:
        if not SAFE_NAME.match(file):
            raise ValueError("bad file name")
        org = load_org(self.orgs_dir / f"{file}.yaml")
        return {"file": file, "config": self.editable(org), "analysis": self.analyze_org(org)}

    @staticmethod
    def editable(org: OrgConfig) -> dict:
        data = org.model_dump(exclude={"levels", "tree"})
        data["tree"] = org.tree_dict()
        return data

    def save(self, file: str, config: dict) -> dict:
        if not SAFE_NAME.match(file):
            raise ValueError("file name may only contain letters, digits, '-' and '_'")
        org = OrgConfig.model_validate(config)
        path = self.orgs_dir / f"{file}.yaml"
        path.write_text(org.to_yaml())
        return {"file": file, "path": str(path), "form": "levels" if org.is_uniform() else "tree"}

    # ---- analysis ----
    def call_cost(self, model: str, effort: str | None) -> float | None:
        try:
            spec = self.catalog.resolve(model)
        except Exception:
            return None
        inp, out, cr, cw = spec.price.rates(6000)
        return 4000 * cr + 2000 * cw + TYPICAL_OUTPUT.get(effort, 1500) * out

    def analyze(self, config: dict) -> dict:
        try:
            org = OrgConfig.model_validate(config)
        except (ValidationError, ValueError) as e:
            return {"ok": False, "error": _errors(e)}
        return self.analyze_org(org)

    def analyze_org(self, org: OrgConfig) -> dict:
        nodes, unknown = [], set()
        for n in org.nodes():
            st = n.settings
            try:
                spec = self.catalog.resolve(st.model)
                provider, price = spec.provider, [spec.price.input, spec.price.output]
            except Exception:
                provider, price = "unknown", None
                unknown.add(st.model)
            nodes.append({
                "name": n.name, "path": n.path, "depth": n.depth, "role": n.role, "parent": n.parent.name if n.parent else None,
                "children": [c.name for c in n.children], "manager": bool(n.children),
                "model": st.model, "effort": st.effort, "provider": provider, "price": price,
                "max_tokens": st.output_cap(), "est_call_cost": self.call_cost(st.model, st.effort),
                "can_do_work": st.can_do_work, "review_tools": st.review_tools, "prompt": st.prompt,
                "subtree": sum(1 for _ in n.walk()) - 1,
            })
        wikis = [{"id": w.id, "name": w.spec.name, "scope": w.spec.scope, "owner": w.owner, "label": w.label(),
                  "members": w.members, "readers": sorted(w.readers), "writers": sorted(w.writers)}
                 for w in wiki_instances(org)]
        est_round = sum(x["est_call_cost"] or 0 for x in nodes)
        return {"ok": True, "metrics": org.metrics(), "describe": org.describe(), "nodes": nodes,
                "layers": org.layer_roles(), "contacts": all_contacts(org), "wikis": wikis,
                "est_round_cost": est_round, "unknown_models": sorted(unknown), "yaml": org.to_yaml()}

    def generate(self, body: dict) -> dict:
        depth = int(body.get("depth", 2))
        fields = {k: v for k, v in body.items() if k in GenerateSpec.model_fields and k not in ("depths", "include_solo")}
        spec = GenerateSpec.model_validate({**fields, "depths": [max(depth, 1)], "include_solo": False})
        if depth == 0:
            top = spec.ladder[0]
            org = OrgConfig.model_validate({"name": spec.name, "levels": [
                {"role": top.role or "solo", "model": top.model, "effort": top.effort}]})
        else:
            org = spec.build()[0]
            org = org.model_copy(update={"name": spec.name})
            org = OrgConfig.model_validate(org.model_dump(exclude_none=True))
        return {"config": self.editable(org), "analysis": self.analyze_org(org)}

    # ---- runs ----
    def start_run(self, body: dict) -> dict:
        from ..providers import ProviderRegistry
        from ..runner import run_one
        from ..tasks import make_task, parse_task_spec
        from ..tracing import Tracer

        org = OrgConfig.model_validate(body["config"])
        family, params = parse_task_spec(body.get("task") or "qa:kind=chain,hops=2")
        task = make_task(family, params, int(body.get("seed") or 1))
        mock = bool(body.get("mock", True))
        budget = float(body.get("budget") or 0.5)
        if not mock:
            if not body.get("confirm_spend"):
                raise ValueError("real runs need confirm_spend=true")
            providers = {self.catalog.resolve(n.settings.model).provider for n in org.nodes()}
            keys = self.catalog_json()["keys"]
            missing = [p for p in providers if p in keys and not keys[p]]
            if missing:
                raise ValueError(f"missing API key for: {', '.join(missing)} (add it to .env and restart)")
        run_key = f"ui{int(time.time() * 1000)}"
        rec = {"key": run_key, "status": "running", "events": [], "states": {}, "costs": {}, "total_cost": 0.0,
               "summary": None, "error": None, "org": org.name, "task": task.task_id, "mock": mock,
               "budget": budget, "started": time.time()}
        with self.lock:
            self.runs[run_key] = rec

        def listener(ev: dict) -> None:
            with self.lock:
                if ev["type"] == "state":
                    st = ev["state"]
                    if st == "done" and ev["data"].get("status") not in (None, "submitted"):
                        st = ev["data"]["status"]  # turn_limit, error, refused...
                    rec["states"][ev["node"]] = st
                elif ev["type"] == "call":
                    rec["costs"][ev["node"]] = rec["costs"].get(ev["node"], 0.0) + ev["cost"]
                    rec["total_cost"] = ev["total_cost"]
                if ev["type"] != "call":
                    rec["events"].append(ev)

        def worker() -> None:
            async def go():
                registry = ProviderRegistry(mock=mock)
                tracer = Tracer(self.results_dir / "orgbench.db")
                try:
                    return await run_one(org, task, catalog=self.catalog, registry=registry, tracer=tracer,
                                         results_dir=self.results_dir, run_cap=budget, listener=listener)
                finally:
                    await registry.aclose()
                    tracer.close()
            try:
                summary = asyncio.run(go())
                with self.lock:
                    rec["summary"] = {k: v for k, v in summary.items() if k != "fatal"}
                    rec["status"] = "finished"
            except Exception as e:
                with self.lock:
                    rec["status"], rec["error"] = "error", f"{e}\n{traceback.format_exc()[-1500:]}"

        threading.Thread(target=worker, daemon=True).start()
        return {"run": run_key, "task": task.task_id, "brief": task.brief[:1500]}

    def run_state(self, key: str, since: int) -> dict:
        with self.lock:
            rec = self.runs.get(key)
            if rec is None:
                raise KeyError(key)
            return {**{k: v for k, v in rec.items() if k != "events"}, "events": rec["events"][since:],
                    "next": len(rec["events"])}


def make_handler(state: UIState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "orgbench-ui"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(status, json.dumps(obj, default=str).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            try:
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/"):])
                if path == "/api/catalog":
                    return self._json(state.catalog_json())
                if path == "/api/orgs":
                    return self._json(state.list_orgs())
                if path.startswith("/api/orgs/"):
                    return self._json(state.load(path.rsplit("/", 1)[1]))
                if path.startswith("/api/runs/"):
                    since = int((parse_qs(url.query).get("since") or ["0"])[0])
                    return self._json(state.run_state(path.rsplit("/", 1)[1], since))
                self._json({"error": "not found"}, 404)
            except FileNotFoundError:
                self._json({"error": "not found"}, 404)
            except KeyError as e:
                self._json({"error": f"unknown {e}"}, 404)
            except Exception as e:
                self._json({"error": _errors(e)}, 400)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/analyze":
                    return self._json(state.analyze(body.get("config") or {}))
                if path == "/api/generate":
                    return self._json(state.generate(body))
                if path.startswith("/api/orgs/"):
                    return self._json(state.save(path.rsplit("/", 1)[1], body.get("config") or {}))
                if path == "/api/runs":
                    return self._json(state.start_run(body))
                if path == "/api/yaml":
                    org = OrgConfig.model_validate(yaml.safe_load(body.get("yaml") or ""))
                    return self._json({"config": state.editable(org), "analysis": state.analyze_org(org)})
                self._json({"error": "not found"}, 404)
            except Exception as e:
                self._json({"error": _errors(e)}, 400)

        def _static(self, rel: str) -> None:
            p = (STATIC / rel).resolve()
            if not p.is_relative_to(STATIC.resolve()) or not p.is_file():
                raise FileNotFoundError(rel)
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self._send(200, p.read_bytes(), ctype)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, orgs_dir: Path | None = None,
          results_dir: Path | None = None, models: str | None = None) -> ThreadingHTTPServer:
    catalog = Catalog.load(models)
    state = UIState(orgs_dir or CONFIG_DIR / "orgs", results_dir or DEFAULT_RESULTS_DIR, catalog)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.daemon_threads = True
    return httpd


__all__ = ["serve", "UIState"]
