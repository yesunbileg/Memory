"""Contract tests for the installable local integrations. No user config is loaded."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/ai-memory"
INSTALL = ROOT / "config/install_hooks.py"


def setup(tmp_path):
    settings_dir = tmp_path / "config"
    env = {**os.environ, "XDG_CONFIG_HOME": str(settings_dir), "HOME": str(tmp_path)}
    cli = tmp_path / "archive cli"
    cli.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    cli.chmod(0o700)
    db = tmp_path / "archive db.sqlite"
    db.touch()
    source = tmp_path / "opencode db.sqlite"
    source.touch()
    cmd = [sys.executable, str(INSTALL), "--cli", str(cli), "--db", str(db)]
    return settings_dir, env, cmd, db, source


def test_installed_targets_resolve_from_any_cwd(tmp_path):
    settings_dir, env, cmd, db, source = setup(tmp_path)
    cmd += ["--opencode-db", str(source), "--account", "test-user"]
    subprocess.run(cmd, cwd=tmp_path, env=env, check=True)
    subprocess.run(cmd, cwd=tmp_path, env=env, check=True)
    settings = tmp_path / ".config/ai-memory/hooks.json"
    assert settings.stat().st_mode & 0o777 == 0o600
    assert (settings_dir / "opencode/plugins/ai-memory.js").is_file()
    catalog = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    assert len(catalog["plugins"]) == 1
    assert (ROOT / catalog["plugins"][0]["source"]["path"]).resolve() == PLUGIN
    manifest = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())
    assert manifest["name"] == catalog["plugins"][0]["name"]
    hooks = json.loads((PLUGIN / manifest["hooks"]).read_text())
    assert set(hooks["hooks"]) == {"Stop"}
    handler = hooks["hooks"]["Stop"][0]["hooks"][0]
    proc = subprocess.run(
        handler["command"],
        shell=True,
        cwd=tmp_path,
        env=dict(env, PLUGIN_ROOT=str(PLUGIN)),
        input="{}",
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(proc.stdout) == ["--db", str(db), "hook", "codex", "--account", "test-user"]
    mcp = json.loads((PLUGIN / manifest["mcpServers"]).read_text())["mcpServers"]["ai_memory"]
    args = mcp["args"]
    proc = subprocess.run(
        [mcp["command"], *args],
        env=env,
        cwd=PLUGIN / mcp["cwd"],
        capture_output=True,
        check=True,
        text=True,
    )
    assert json.loads(proc.stdout) == ["--db", str(db), "mcp"]


def test_refuses_conflicts_before_installing(tmp_path):
    settings_dir, env, cmd, _, source = setup(tmp_path)
    plugin = settings_dir / "opencode/plugins/ai-memory.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("my plugin")
    p = subprocess.run(cmd + ["--opencode-db", str(source)], env=env, capture_output=True)
    assert p.returncode != 0
    assert plugin.read_text() == "my plugin"
    assert not (tmp_path / ".config/ai-memory/hooks.json").exists()


def test_rejects_relative_archive_and_foreign_settings(tmp_path):
    settings_dir, env, cmd, db, _ = setup(tmp_path)
    p = subprocess.run([*cmd[:-1], db.name], cwd=tmp_path, env=env, capture_output=True)
    assert p.returncode != 0
    settings = tmp_path / ".config/ai-memory/hooks.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"server":"mine"}')
    assert subprocess.run(cmd, env=env, capture_output=True).returncode != 0
    assert settings.read_text() == '{"server":"mine"}'


@pytest.mark.parametrize("host", ["v1", "v2"])
def test_installed_opencode_bridge_only_forwards_idle_session_identifiers(tmp_path, host):
    settings_dir, env, cmd, db, source = setup(tmp_path)
    log = tmp_path / "calls.jsonl"
    cli = Path(cmd[3])
    cli.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "with open(os.environ['TEST_LOG'], 'a') as f:\n"
        " print(json.dumps([sys.argv[1:], json.load(sys.stdin)]), file=f)\n"
    )
    cmd += ["--opencode-db", str(source)]
    subprocess.run(cmd, env=env, check=True)
    bridge = settings_dir / "opencode/plugins/ai-memory.js"
    fixture = r"""
import { pathToFileURL } from 'node:url';
const plugin = (await import(pathToFileURL(process.argv[1]))).default;
const unrelated = {type: 'message.updated', properties: {sessionID: 'not-selected'}, data: {sessionID: 'not-selected'}};
if (process.argv[2] === 'v1') {
  const {event} = await plugin.server();
  await event({event: unrelated});
  await event({event: {type: 'session.idle', properties: {sessionID: 'selected', body: 'PRIVATE'}}});
} else {
  let signal, finished;
  const done = new Promise((resolve) => { finished = resolve; });
  async function* subscribe(options) {
    if (options.types.join(',') !== 'session.idle') throw Error('not filtered');
    signal = options.signal;
    yield unrelated;
    yield {type: 'session.idle', data: {sessionID: 'selected', body: 'PRIVATE'}};
    finished();
  }
  const cleanup = plugin.setup({event: {subscribe}});
  await done;
  cleanup();
  if (!signal.aborted) throw Error('subscription not disposed');
}
"""
    p = subprocess.run(
        ["node", "--input-type=module", "-e", fixture, str(bridge), host],
        cwd=tmp_path,
        env=dict(env, TEST_LOG=str(log)),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert p.returncode == 0, p.stderr
    assert p.stdout == ""
    (record,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert record == [
        ["--db", str(db), "hook", "opencode", "--source", str(source), "--account", "local"],
        {"event": "session.idle", "session_id": "selected"},
    ]
