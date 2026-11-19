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
            "popup.html",
            "popup.css",
            "popup.js",
            "page-picker.js",
            "providers/chatgpt.js",
            "providers/claude.js",
            "providers/gemini.js",
            "providers/selection.js",
            "lib/common.js",
            "icons/icon-128.png",
        ):
            assert archive.read(name) == (source / name).read_bytes()
        assert archive.read("LICENSE") == (source.parent / "LICENSE").read_bytes()
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["background"]["service_worker"] in names
        assert "options_page" not in manifest and "options_ui" not in manifest
        trusted_sites = {
            "https://chatgpt.com/*",
            "https://claude.ai/*",
            "https://gemini.google.com/*",
        }
        assert set(manifest["host_permissions"]) == trusted_sites
        (picker,) = manifest["content_scripts"]
        assert set(picker["matches"]) == trusted_sites
        assert picker.get("world", "ISOLATED") == "ISOLATED"
        assert picker.get("all_frames", False) is False
        assert not any(
            picker.get(k) for k in ("match_about_blank", "match_origin_as_fallback", "css")
        )
        assert picker["js"] == ["page-picker-routes.js", "page-picker.js"]
        assert not manifest.get("externally_connectable") and not manifest.get(
            "web_accessible_resources"
        )
        assert not ({"scripting", "tabs", "activeTab", "downloads"} & set(manifest["permissions"]))
        assert {name for name in names if name.endswith(".html")} == {"popup.html"}
        assert (
            not {
                "options.html",
                "options.js",
                "popup-picker.js",
                "sidebar-picker.js",
                "sidebar-picker.css",
            }
            & names
        )
        runtime = b"\n".join(
            archive.read(name) for name in names if name.endswith((".html", ".js"))
        )
        for old_surface in (b"openOptionsPage(", b"chrome.tabs.create(", b"chrome.windows.create("):
            assert old_surface not in runtime
        assert manifest["action"]["default_popup"] in names
        assert all(icon in names for icon in manifest["icons"].values())
        assert not any(name.startswith(("test/", "data/")) for name in names)
        assert "extension-connection.json" not in names
        assert "storage_state.json" not in names
        assert sentinel.encode() not in b"".join(archive.read(name) for name in names)


@pytest.mark.parametrize(
    "missing", ["icons/icon-128.png", "popup.js", "page-picker.js", "providers/chatgpt.js"]
)
def test_extension_package_rejects_missing_runtime_dependencies(tmp_path, missing):
    source = package_source(tmp_path)
    (source / missing).unlink()
    output = tmp_path / "invalid.zip"
    result = package(source, output)
    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize("missing", [None, "sidebar-fixture.js", "sidebar-fixture.css"])
def test_extension_package_validates_all_manifest_content_scripts(tmp_path, missing):
    source = package_source(tmp_path)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["content_scripts"] = [
        {
            "matches": ["https://chatgpt.com/*"],
            "js": ["sidebar-fixture.js"],
            "css": ["sidebar-fixture.css"],
        }
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assets = {"sidebar-fixture.js": "void 0;\n", "sidebar-fixture.css": ":root {}\n"}
    for name, content in assets.items():
        if name != missing:
            (source / name).write_text(content, encoding="utf-8")
    output = tmp_path / "extension.zip"
    result = package(source, output)
    if missing is not None:
        assert result.returncode != 0
        assert missing in result.stderr
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        with zipfile.ZipFile(output) as archive:
            for name, content in assets.items():
                assert archive.read(name) == content.encode()


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
