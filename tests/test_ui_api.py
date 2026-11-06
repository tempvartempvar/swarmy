"""The UI server's JSON API (no browser needed)."""

import json
import shutil
import threading
import time
import urllib.request

import pytest

from orgbench.paths import CONFIG_DIR
from orgbench.ui.server import serve


@pytest.fixture
def server(tmp_path, monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    orgs = tmp_path / "orgs"
    shutil.copytree(CONFIG_DIR / "orgs", orgs)
    httpd = serve("127.0.0.1", 0, orgs, tmp_path / "results")
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
        assert b"org designer" in r.read()
    with urllib.request.urlopen(base + "/static/app.js") as r:
        assert r.headers["content-type"].startswith("text/javascript") or "javascript" in r.headers["content-type"]
    status, cat = call("/api/catalog")
    assert status == 200 and any(m["alias"] == "opus" for m in cat["models"]) and cat["tasks"]


def test_load_analyze_edit_save(server):
    call, _, orgs = server
    status, data = call("/api/orgs/two-tier")
    assert status == 200
    cfg = data["config"]
    assert "tree" in cfg and data["analysis"]["metrics"]["shape"] == "1-2-6"
    # per-position edit: one manager gets an extra report on a different model
    cfg["tree"]["children"][0]["children"].append({"role": "worker", "model": "haiku"})
    cfg["knowledge"] = {"wikis": [{"name": "handbook", "scope": "org", "publish_brief": True}]}
    cfg["communication"] = {"peers": True, "skip_up": 1}
    status, a = call("/api/analyze", {"config": cfg})
    assert status == 200 and a["ok"], a
    assert a["metrics"]["shape"] == "1-2-7" and not a["metrics"]["uniform"]
    assert {c["kind"] for c in a["contacts"]["worker-1.4"]} == {"peer", "skip_up"}
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
                                        "ladder": [{"role": "lead", "model": "sonnet"}, {"role": "manager", "model": "haiku"}],
                                        "worker": {"role": "worker", "model": "gpt-oss-120b"}})
    assert status == 200 and g["analysis"]["metrics"]["shape"] == "1-2-4-8"
    status, y = call("/api/yaml", {"yaml": g["analysis"]["yaml"]})
    assert status == 200 and y["analysis"]["metrics"]["shape"] == "1-2-4-8"


def test_mock_run_with_live_state(server):
    call, _, _ = server
    _, data = call("/api/orgs/team-board")
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
