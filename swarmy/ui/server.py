"""Local web UI: an interactive org chart editor with live test runs.

Stdlib-only HTTP server. The browser holds the org being edited (as an explicit tree) and asks
`/api/analyze` for everything derived from it - names, metrics, contacts, wiki membership - so the
chart always shows exactly the rules the engine will run.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
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
from ..ranks import GLOSS, OFFICER_LADDER, SOLO_RANK, WORKER_RANK, management_ranks
from ..rules import KIND_LABELS, all_contacts, wiki_instances

STATIC = Path(__file__).parent / "static"
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
# Rough output tokens per call by effort, for the cost hint only.
TYPICAL_OUTPUT = {None: 1500, "low": 700, "medium": 1500, "high": 3000, "xhigh": 5000, "max": 8000}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RUN_ID = re.compile(r"^run_[0-9]{8}-[0-9]{6}_[0-9a-f]{6}$")

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
    def __init__(self, orgs_dir: Path, results_dir: Path, catalog: Catalog, models_file: str | None = None):
        self.models_file = models_file
        self.orgs_dir = orgs_dir
        self.results_dir = results_dir
        self.catalog = catalog
        self.runs: dict[str, dict] = {}
        self.stores: dict[str, dict] = {}
        self.live_ids: dict[str, str] = {}  # run_id -> UI run key, for runs started here
        self.lock = threading.Lock()

    # ---- catalog ----
    def catalog_json(self) -> dict:
        models = []
        for alias, spec in self.catalog.models.items():
            ep = spec.endpoint_cfg
            models.append({"alias": alias, "endpoint": spec.endpoint, "kind": spec.kind, "model": spec.model,
                           "input": spec.rate.input, "output": spec.rate.output, "price_known": spec.price_known,
                           "free": spec.rate.input == 0 and spec.rate.output == 0 and spec.kind != "mock",
                           "local": bool(ep and ep.is_local), "rpm": spec.rpm, "source": spec.source})
        return {"models": models, "efforts": EFFORT_LEVELS, "kinds": KIND_LABELS,
                "endpoints": self.endpoints_json(),
                "keys": {n: ep.key_present for n, ep in self.catalog.endpoints.items()},
                "tasks": TASK_FAMILIES,
                "ranks": {"ladder": OFFICER_LADDER, "worker": WORKER_RANK, "solo": SOLO_RANK, "gloss": GLOSS,
                          "by_depth": {d: management_ranks(d) for d in range(1, 9)}}}

    def endpoints_json(self) -> list[dict]:
        return [{"name": n, "type": ep.type, "base_url": ep.base_url, "api_key_env": ep.api_key_env,
                 "key_present": ep.key_present, "local": ep.is_local, "source": ep.source, "notes": ep.notes,
                 "config": ep.model_dump(exclude={"name"}, exclude_defaults=True)}
                for n, ep in self.catalog.endpoints.items()]

    # ---- models & endpoints ----
    def models_json(self) -> dict:
        local = self.catalog.local_data()
        return {"endpoints": self.endpoints_json(), "models": self.catalog_json()["models"],
                "local": {"endpoints": local.get("endpoints") or {}, "models": local.get("models") or {}},
                "local_path": str(self.catalog.local_path)}

    def save_models(self, body: dict) -> dict:
        self.catalog.save_local(body.get("endpoints") or {}, body.get("models") or {})
        self.catalog = Catalog.load(self.models_file)
        return self.models_json()

    def test_model(self, body: dict) -> dict:
        from ..providers import probe

        spec = self.catalog.resolve(body["model"])
        return asyncio.run(probe(spec))

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
        inp, out, cr, cw = spec.rate.rates(6000)
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
                provider, price = spec.endpoint, ([spec.rate.input, spec.rate.output] if spec.price_known else None)
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
                {"role": top.role or SOLO_RANK, "model": top.model, "effort": top.effort}]})
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
        if (body.get("mission") or "").strip():
            task = make_task("mission", {"text": body["mission"]}, int(body.get("seed") or 1))
        else:
            family, params = parse_task_spec(body.get("task") or "qa:kind=chain,hops=2")
            task = make_task(family, params, int(body.get("seed") or 1))
        mock = bool(body.get("mock", True))
        budget = float(body.get("budget") or 0.5)
        if not mock:
            if not body.get("confirm_spend"):
                raise ValueError("real runs need confirm_spend=true")
            missing = sorted({f"{spec.endpoint} ({spec.endpoint_cfg.api_key_env})"
                              for spec in (self.catalog.resolve(n.settings.model) for n in org.nodes())
                              if spec.endpoint_cfg and not spec.endpoint_cfg.key_present})
            if missing:
                raise ValueError(f"missing API key for: {', '.join(missing)} (add it to .env and restart)")
        run_key = f"ui{int(time.time() * 1000)}"
        rec = {"key": run_key, "status": "running", "events": [], "states": {}, "costs": {}, "total_cost": 0.0,
               "summary": None, "error": None, "org": org.name, "task": task.task_id, "mock": mock,
               "budget": budget, "started": time.time(), "run_id": None, "org_json": org.model_dump_json(),
               "family": task.family}
        stores: dict = {}

        def on_start(ctx) -> None:
            stores["wiki"] = ctx.wiki
            stores["workspace"] = ctx.workspace
            with self.lock:
                rec["run_id"] = ctx.run_id
                self.live_ids[ctx.run_id] = run_key
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
                tracer = Tracer(self.results_dir / "swarmy.db")
                try:
                    return await run_one(org, task, catalog=self.catalog, registry=registry, tracer=tracer,
                                         results_dir=self.results_dir, run_cap=budget, listener=listener,
                                         on_start=on_start)
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

        self.stores[run_key] = stores
        threading.Thread(target=worker, daemon=True).start()
        return {"run": run_key, "task": task.task_id, "brief": task.brief[:1500]}

    # ---- run explorer ----
    def _run_dir(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise ValueError("bad run id")
        return self.results_dir / "runs" / run_id

    def explorer_runs(self) -> list[dict]:
        out, seen = [], set()
        with self.lock:
            live = [(rid, self.runs[key]) for rid, key in self.live_ids.items()]
        for rid, rec in live:
            if rec["status"] == "running":
                seen.add(rid)
                out.append({"run_id": rid, "org": rec["org"], "task": rec["task"], "status": "running",
                            "score": None, "family": rec.get("family"), "mtime": rec["started"], "live": True})
        runs_dir = self.results_dir / "runs"
        if runs_dir.exists():
            for f in runs_dir.glob("run_*/result.json"):
                rid = f.parent.name
                if rid in seen:
                    continue
                try:
                    meta = json.loads(f.read_text())
                except Exception:
                    continue
                out.append({"run_id": rid, "org": meta.get("org_name"), "task": meta.get("task_id"),
                            "status": meta.get("status"), "score": meta.get("score"),
                            "family": (meta.get("task_id") or "").split("/")[0], "mtime": f.stat().st_mtime,
                            "has_wikis": (f.parent / "wikis" / "wikis.json").exists()})
        out.sort(key=lambda r: -r["mtime"])
        return out[:100]

    def explorer_orders(self, run_id: str) -> dict:
        from ..orders import load_orders
        from ..tracing import Tracer

        self._run_dir(run_id)
        key = self.live_ids.get(run_id)
        org_json = self.runs[key]["org_json"] if key else None
        tracer = Tracer(self.results_dir / "swarmy.db")
        try:
            return load_orders(tracer, run_id, org_json)
        finally:
            tracer.close()

    def explorer_wikis(self, run_id: str) -> dict:
        from ..wiki import load

        run_dir = self._run_dir(run_id)
        key = self.live_ids.get(run_id)
        store = self.stores.get(key, {}).get("wiki") if key else None
        if store is not None:
            return {"run_id": run_id, **store.to_json()}
        data = load(run_dir)
        return {"run_id": run_id, **(data or {"instances": []})}

    def explorer_files(self, run_id: str) -> dict:
        ws = self._run_dir(run_id) / "workspace"
        files = []
        if ws.exists():
            for p in sorted(ws.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts and not any(x.startswith(".") for x in p.relative_to(ws).parts):
                    files.append({"path": str(p.relative_to(ws)), "bytes": p.stat().st_size})
        return {"run_id": run_id, "files": files[:500]}

    def explorer_file(self, run_id: str, rel: str) -> dict:
        ws = (self._run_dir(run_id) / "workspace").resolve()
        p = (ws / rel).resolve()
        if not p.is_relative_to(ws) or not p.is_file():
            raise FileNotFoundError(rel)
        raw = p.read_bytes()[:300_000]
        try:
            text = raw.decode("utf-8")
            binary = False
        except UnicodeDecodeError:
            text, binary = "", True
        return {"path": rel, "bytes": p.stat().st_size, "text": text, "binary": binary,
                "truncated": p.stat().st_size > len(raw)}

    def run_state(self, key: str, since: int) -> dict:
        with self.lock:
            rec = self.runs.get(key)
            if rec is None:
                raise KeyError(key)
            return {**{k: v for k, v in rec.items() if k != "events"}, "events": rec["events"][since:],
                    "next": len(rec["events"])}


def make_handler(state: UIState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "swarmy-ui"

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
                if path == "/api/models":
                    return self._json(state.models_json())
                if path == "/api/explorer/runs":
                    return self._json(state.explorer_runs())
                m = re.match(r"^/api/explorer/([^/]+)/(orders|wikis|files|file)$", path)
                if m:
                    rid, what = m.groups()
                    if what == "file":
                        return self._json(state.explorer_file(rid, (parse_qs(url.query).get("path") or [""])[0]))
                    return self._json(getattr(state, f"explorer_{what}")(rid))
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
                if path == "/api/models/local":
                    return self._json(state.save_models(body))
                if path == "/api/models/test":
                    return self._json(state.test_model(body))
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
    state = UIState(orgs_dir or CONFIG_DIR / "orgs", results_dir or DEFAULT_RESULTS_DIR, catalog, models)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.daemon_threads = True
    return httpd


__all__ = ["serve", "UIState"]
