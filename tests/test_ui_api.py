"""The UI server's JSON API (no browser needed)."""

import json
import shutil
import threading
import time
import urllib.request

import pytest

from swarmy.paths import CONFIG_DIR
from swarmy.ui.server import serve


@pytest.fixture
def server(tmp_path, monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    orgs = tmp_path / "orgs"
    shutil.copytree(CONFIG_DIR / "orgs", orgs)
    shutil.copy(CONFIG_DIR / "models.yaml", tmp_path / "models.yaml")  # keep models.local.yaml out of the repo
    httpd = serve("127.0.0.1", 0, orgs, tmp_path / "results", str(tmp_path / "models.yaml"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield call, base, orgs
    httpd.shutdown()


def test_static_and_catalog(server):
    call, base, _ = server
    with urllib.request.urlopen(base + "/") as r:
        assert b"order of battle" in r.read()
    with urllib.request.urlopen(base + "/static/app.js") as r:
        assert r.headers["content-type"].startswith("text/javascript") or "javascript" in r.headers["content-type"]
    status, cat = call("/api/catalog")
    assert status == 200 and any(m["alias"] == "opus" for m in cat["models"]) and cat["tasks"]


def test_load_analyze_edit_save(server):
    call, _, orgs = server
    status, data = call("/api/orgs/regiment")
    assert status == 200
    cfg = data["config"]
    assert "tree" in cfg and data["analysis"]["metrics"]["shape"] == "1-2-6"
    # per-position edit: one manager gets an extra report on a different model
    cfg["tree"]["children"][0]["children"].append({"role": "private", "model": "haiku"})
    cfg["knowledge"] = {"wikis": [{"name": "handbook", "scope": "org", "publish_brief": True}]}
    cfg["communication"] = {"peers": True, "skip_up": 1}
    status, a = call("/api/analyze", {"config": cfg})
    assert status == 200 and a["ok"], a
    assert a["metrics"]["shape"] == "1-2-7" and not a["metrics"]["uniform"]
    assert {c["kind"] for c in a["contacts"]["private-1.4"]} == {"peer", "skip_up"}
    assert a["wikis"][0]["id"] == "handbook" and len(a["wikis"][0]["members"]) == 10
    status, saved = call("/api/orgs/edited", {"config": cfg})
    assert status == 200 and saved["form"] == "tree"
    assert "tree:" in (orgs / "edited.yaml").read_text()


def test_invalid_config_reports_error(server):
    call, _, _ = server
    status, a = call("/api/analyze", {"config": {"name": "x", "tree": {"role": "a"}}})
    assert status == 200 and not a["ok"] and "model" in a["error"]
    status, err = call("/api/orgs/../../etc", {"config": {}})
    assert status in (400, 404)


def test_generate_and_yaml_roundtrip(server):
    call, _, _ = server
    status, g = call("/api/generate", {"name": "g", "workers": 8, "depth": 3,
                                        "ladder": [{"model": "sonnet"}, {"model": "haiku"}],
                                        "worker": {"model": "gpt-oss-120b"}})
    assert status == 200 and g["analysis"]["metrics"]["shape"] == "1-2-4-8"
    assert g["analysis"]["layers"] == ["field-marshal", "colonel", "captain", "private"]
    status, y = call("/api/yaml", {"yaml": g["analysis"]["yaml"]})
    assert status == 200 and y["analysis"]["metrics"]["shape"] == "1-2-4-8"


def test_mock_run_with_live_state(server):
    call, _, _ = server
    _, data = call("/api/orgs/regiment-mess")
    status, started = call("/api/runs", {"config": data["config"], "task": "code:n=4,coupling=0", "mock": True,
                                         "budget": 0.2})
    assert status == 200
    for _ in range(100):
        _, st = call(f"/api/runs/{started['run']}?since=0")
        if st["status"] != "running":
            break
        time.sleep(0.1)
    assert st["status"] == "finished", st.get("error")
    assert st["summary"]["status"] == "completed"
    assert set(st["states"].values()) == {"done"}
    assert any(e["type"] == "event" and e["kind"] == "assign" for e in st["events"])


def test_real_run_requires_confirmation(server):
    call, _, _ = server
    _, data = call("/api/orgs/solo-haiku")
    status, err = call("/api/runs", {"config": data["config"], "mock": False})
    assert status == 400 and "confirm" in err["error"]


def _wait(call, key):
    for _ in range(150):
        _, st = call(f"/api/runs/{key}?since=0")
        if st["status"] != "running":
            return st
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_mission_run_and_explorer(server):
    call, _, _ = server
    _, data = call("/api/orgs/general-staff")
    status, started = call("/api/runs", {"config": data["config"], "mock": True, "budget": 0.2,
                                         "mission": "Build a simulation of orbital dynamics across LEO, MEO and GEO."})
    assert status == 200 and started["task"].startswith("mission/build-a-simulation")
    st = _wait(call, started["run"])
    assert st["status"] == "finished" and st["summary"]["score"] is None and st["summary"]["status"] == "completed"
    rid = st["run_id"]
    _, runs = call("/api/explorer/runs")
    assert runs[0]["run_id"] == rid
    _, orders = call(f"/api/explorer/{rid}/orders")
    by_id = {o["id"]: o for o in orders["orders"]}
    assert by_id[0]["from"] == "client" and by_id[0]["to"] == "field-marshal" and "orbital" in by_id[0]["text"]
    firsts = [o for o in orders["orders"] if o["parent"] == 0]
    assert {o["to"] for o in firsts} == {"intelligence-colonel", "engineer-colonel"}
    assert all(o["report"] for o in orders["orders"]) and by_id[0]["report"]["status"] == "submitted"
    assert "field-marshal" in orders["goals"] and "engineer-colonel" in orders["goals"]
    status, wikis = call(f"/api/explorer/{rid}/wikis")
    assert status == 200 and {i["id"] for i in wikis["instances"]} >= {"orders", "unit-log/engineer-colonel"}
    page = next(p for i in wikis["instances"] if i["id"] == "orders" for p in i["pages"] if p["title"] == "Plan")
    assert page["revisions"][0]["role"] == "field-marshal"
    assert wikis["storage"] == "git" and page["revisions"][0]["html"].startswith("<") and page["revisions"][0]["diff"]
    assert by_id[0]["html"]["text"] and by_id[0]["report"]["html"]
    status, log = call(f"/api/explorer/{rid}/log")
    assert status == 200 and log["exists"] and log["end_reason"] and log["status"]
    assert log["entries"][0]["level"] == "INFO" and any("run ended" in e["text"] for e in log["entries"])
    assert {e["level"] for e in log["entries"]} <= {"DEBUG", "INFO", "WARNING", "ERROR"}
    status, files = call(f"/api/explorer/{rid}/files")
    assert status == 200 and "files" in files
    status, _ = call("/api/explorer/../../etc/orders")
    assert status in (400, 404)


def test_models_api_local_overrides_and_test(server):
    call, _, _ = server
    _, m = call("/api/models")
    assert {e["name"] for e in m["endpoints"]} >= {"anthropic", "openrouter", "openai", "ollama", "mock"}
    status, saved = call("/api/models/local", {
        "endpoints": {"vllm": {"type": "openai", "base_url": "http://gpu-box:8000/v1", "api_key_env": None}},
        "models": {"big-local": {"endpoint": "vllm", "model": "Qwen/Qwen3-32B"}}})
    assert status == 200
    assert any(e["name"] == "vllm" and e["source"] == "local" for e in saved["endpoints"])
    assert any(x["alias"] == "big-local" and x["endpoint"] == "vllm" for x in saved["models"])
    _, cat = call("/api/catalog")
    assert any(x["alias"] == "big-local" for x in cat["models"])
    status, err = call("/api/models/local", {"endpoints": {"bad": {"type": "openai", "api_key": "sk-123"}}, "models": {}})
    assert status == 400
    status, probe = call("/api/models/test", {"model": "mock"})
    assert status == 200 and probe["ok"]
