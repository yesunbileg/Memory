import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def package_source(tmp_path):
    source = tmp_path / "hstry/extension"
    shutil.copytree(ROOT / "vendor/hstry/extension", source)
    shutil.copyfile(ROOT / "vendor/hstry/LICENSE", source.parent / "LICENSE")
    return source


def package(source, output):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/package_extension.py"),
            "--source",
            str(source),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_extension_zip_loads_from_root_and_excludes_connection_and_test_data(tmp_path):
    source = package_source(tmp_path)
    sentinel = "SYNTHETIC_PRIVATE_VALUE_DO_NOT_PACKAGE"
    for name in ("ingest.token", "extension-connection.json", "storage_state.json", ".env"):
        (source / name).write_text(sentinel, encoding="utf-8")
    (source / "data").mkdir()
    (source / "data/private.json").write_text(sentinel, encoding="utf-8")
    output = tmp_path / "extension.zip"
    result = package(source, output)
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        for name in (
            "manifest.json",
            "background.js",
            "options.html",
            "options.js",
            "popup.js",
            "providers/chatgpt.js",
            "providers/selection.js",
            "lib/common.js",
            "icons/icon-128.png",
        ):
            assert archive.read(name) == (source / name).read_bytes()
        assert archive.read("LICENSE") == (source.parent / "LICENSE").read_bytes()
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["background"]["service_worker"] in names
        assert manifest["options_page"] in names
        assert manifest["action"]["default_popup"] in names
        assert all(icon in names for icon in manifest["icons"].values())
        assert not any(name.startswith(("test/", "data/")) for name in names)
        assert "extension-connection.json" not in names
        assert "storage_state.json" not in names
        assert sentinel.encode() not in b"".join(archive.read(name) for name in names)


@pytest.mark.parametrize("missing", ["icons/icon-128.png", "options.js", "providers/chatgpt.js"])
def test_extension_package_rejects_missing_runtime_dependencies(tmp_path, missing):
    source = package_source(tmp_path)
    (source / missing).unlink()
    output = tmp_path / "invalid.zip"
    result = package(source, output)
    assert result.returncode != 0
    assert not output.exists()


def test_extension_package_rejects_asset_symlinks_outside_its_source(tmp_path):
    source = package_source(tmp_path)
    private = tmp_path / "private.png"
    private.write_bytes(b"SYNTHETIC_PRIVATE_DATA")
    icon = source / "icons/icon-128.png"
    icon.unlink()
    icon.symlink_to(private)
    output = tmp_path / "invalid.zip"
    result = package(source, output)
    assert result.returncode != 0
    assert not output.exists()
