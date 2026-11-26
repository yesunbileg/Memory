import json
import subprocess
from pathlib import Path

from ai_memory.store import Store


def test_docs_connection_verifies_background_client_before_persisting_mode(tmp_path, monkeypatch):
    from ai_memory import cli, web_docs_background
    from ai_memory.web_docs_publication import DOCS_SETTING

    db = tmp_path / "archive.sqlite"
    auth = tmp_path / "storage.json"
    auth.write_text("fixture")
    with Store(db) as store:
        store.set_setting("notebook_id", "example")
        store.set_setting("storage_path", str(auth))

    calls = []

    class Background:
        def __init__(self, path):
            assert Path(path) == auth

        async def call(self, action):
            calls.append(action)
            with Store(db, existing=True) as snapshot:
                assert not snapshot.get_setting(DOCS_SETTING)
            return {"status": "ready"}

        async def release(self):
            calls.append("close")

    monkeypatch.setattr(web_docs_background, "BackgroundDocsTransport", Background)
    args = cli.parser().parse_args(["--db", str(db), "docs-web-enable"])
    assert cli.execute(args)["docs_web_enabled"] is True
    assert calls == ["ping", "close"]
    with Store(db, existing=True) as store:
        assert store.get_setting(DOCS_SETTING) == "background"


def test_background_worker_imports_cookies_privately_and_reuses_its_context(tmp_path):
    root = Path(__file__).resolve().parents[1]
    setup = tmp_path / "setup.json"
    setup.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "fixture",
                        "value": "private-example",
                        "domain": ".google.com",
                        "path": "/",
                        "secure": False,
                        "sameSite": "None",
                    }
                ],
                "origins": [{"origin": "https://other.example", "localStorage": []}],
            }
        )
    )
    harness = tmp_path / "playwright.cjs"
    harness.write_text("""
exports.chromium = { launch: async options => {
  if (!options.headless) throw Error();
  return {
    newContext: async ({storageState}) => {
      if (storageState.cookies[0].sameSite !== "Lax" || storageState.origins.length) throw Error();
      return { mark: "fixture context" };
    },
    close: async () => {},
  };
}};
""")
    action = tmp_path / "operation.js"
    action.write_text("""
state.count = 1 + (state.count || 0);
console.log("AI_MEMORY_DOCS_RESULT=" + JSON.stringify({count: state.count, context: context.mark}));
""")
    result = subprocess.run(
        [
            "node",
            str(root / "src/ai_memory/web_docs_background.cjs"),
            str(harness),
            "/fixture/brave",
            str(setup),
        ],
        input=f"{action}\n{action}\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=8,
    )
    lines = result.stdout.splitlines()
    assert lines[0] == "ready"
    assert [json.loads(line.split("=", 1)[1]) for line in lines[1:]] == [
        {"count": 1, "context": "fixture context"},
        {"count": 2, "context": "fixture context"},
    ]
    assert "private-example" not in result.stdout + result.stderr


def test_audit_meter_runs_in_worker_and_reports_only_numeric_metadata(tmp_path):
    root = Path(__file__).resolve().parents[1]
    storage = tmp_path / "storage.json"
    storage.write_text(json.dumps({"cookies": [{"name": "fixture", "value": "private-cookie"}]}))
    harness = tmp_path / "playwright.cjs"
    harness.write_text("""
const { EventEmitter } = require("node:events");
exports.chromium = { launch: async () => ({
  newContext: async () => ({
    newCDPSession: async () => Object.assign(new EventEmitter(), {send: async () => {}}),
  }),
  close: async () => {},
}) };
""")
    meter = (root / "scripts/audit_browser.js").read_text()
    scripts = []
    for name, params in [
        ("start", {"action": "start", "marker": "unchanged-synthetic"}),
        ("append", {"action": "phase", "phase": "append"}),
        ("unchanged", {"action": "phase", "phase": "unchanged"}),
        ("report", {"action": "report", "stop": True}),
    ]:
        path = tmp_path / f"{name}.js"
        path.write_text(f"const auditInput = {json.dumps(params)};\n" + meter)
        scripts.append(path)
    operation = tmp_path / "request.js"
    operation.write_text("""
const { EventEmitter } = require("node:events");
const page = new EventEmitter();
await state.memoryDocsAudit.watch(page);
const body = Buffer.from("synthetic-private-body");
const request = {
  url: () => "https://docs.google.com/document/d/synthetic/save?token=synthetic-secret",
  method: () => "POST",
  postDataBuffer: () => body,
  sizes: async () => ({
    requestBodySize: body.length, responseBodySize: 7,
    requestHeadersSize: 13, responseHeadersSize: 12,
  }),
};
page.emit("request", request);
page.emit("requestfinished", request);
console.log('AI_MEMORY_DOCS_RESULT={"status":"ok"}');
""")
    scripts.insert(2, operation)
    result = subprocess.run(
        [
            "node",
            str(root / "src/ai_memory/web_docs_background.cjs"),
            str(harness),
            "/fixture/brave",
            str(storage),
        ],
        input="\n".join(map(str, scripts)) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=8,
    )
    rows = result.stdout.splitlines()
    assert rows[0] == "ready"
    records = [json.loads(row.split("=", 1)[1]) for row in rows[1:]]
    assert all(record["status"] == "ok" for record in records)
    metrics = records[-1]["metrics"]
    assert metrics["append"]["saves"] == 1
    assert metrics["append"]["saveBytes"] == len("synthetic-private-body")
    assert metrics["append"]["measured"] == 1
    assert metrics["unchanged"]["saves"] == 0
    assert metrics["unchanged"]["elapsedMs"] >= 0
    for secret in ("private-cookie", "synthetic-private-body", "synthetic-secret"):
        assert secret not in result.stdout + result.stderr
