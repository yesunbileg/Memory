"""Build a load-unpacked ZIP containing the extension's public runtime files."""

import argparse
import hashlib
import json
import os
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    "manifest.json",
    "*.js",
    "*.html",
    "*.css",
    "providers/*.js",
    "lib/*.js",
    "icons/icon-*.png",
)
JS_IMPORTS = re.compile(r"""(?:\bfrom\s*|\bimport\s*(?:\(\s*)?)["'](\.[^"']+)["']""")
HTML_ASSETS = re.compile(r"""(?:src|href)\s*=\s*["']([^"'#]+)["']""")


def runtime_assets(source: Path, manifest: dict) -> dict[str, Path]:
    if manifest.get("manifest_version") != 3:
        raise ValueError("Expected a Manifest V3 extension")
    action = manifest["action"]
    icons = action.get("default_icon", {})
    stack = [
        "manifest.json",
        manifest["background"]["service_worker"],
        action["default_popup"],
        *manifest.get("icons", {}).values(),
        *(icons.values() if isinstance(icons, dict) else [icons]),
    ]
    # Older releases have a separate options page. Never include an unreferenced one.
    if "options_page" in manifest:
        stack.append(manifest["options_page"])
    content_scripts = manifest.get("content_scripts", [])
    if not isinstance(content_scripts, list):
        raise ValueError("content_scripts must be a list")
    for entry in content_scripts:
        if not isinstance(entry, dict):
            raise ValueError("content_scripts entries must be objects")
        for key in ("js", "css"):
            assets = entry.get(key, [])
            if not isinstance(assets, list) or any(not isinstance(name, str) for name in assets):
                raise ValueError(f"content_scripts {key} must be a list of runtime paths")
            stack.extend(assets)
    allowed = {
        p.relative_to(source).as_posix() for pattern in PATTERNS for p in source.glob(pattern)
    }
    files = {}
    while stack:
        name = stack.pop()
        if name in files:
            continue
        path = source / name
        if name not in allowed or not path.is_file() or path.is_symlink() or path.resolve() != path:
            raise ValueError(f"Missing or unsafe runtime file: {name}")
        files[name] = path
        pattern = (
            JS_IMPORTS if path.suffix == ".js" else HTML_ASSETS if path.suffix == ".html" else None
        )
        if pattern:
            for ref in pattern.findall(path.read_text(encoding="utf-8")):
                if ref.startswith(("https://", "http://", "data:")):
                    continue
                ref = ref.split("?", 1)[0].split("#", 1)[0]
                if ref:
                    stack.append(posixpath.normpath(posixpath.join(posixpath.dirname(name), ref)))
    return files


def build_extension(source: Path, output: Path | None = None) -> dict:
    source = source.expanduser().resolve(strict=True)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    contents = {name: p.read_bytes() for name, p in runtime_assets(source, manifest).items()}
    for name, path in {
        "LICENSE": source.parent / "LICENSE",
        "LICENSE.ai-memory": ROOT / "LICENSE",
    }.items():
        if path.is_symlink():
            raise ValueError("The extension licenses must be regular files")
        contents[name] = path.read_bytes()
    output = (
        (output or ROOT / "dist" / f"ai-memory-extension-{manifest['version']}.zip")
        .expanduser()
        .absolute()
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".extension-", suffix=".zip", dir=output.parent)
    os.close(fd)
    temporary = Path(tmp)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name, content in sorted(contents.items()):
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        # Never overwrite a user file or silently replace a published version.
        # A hard link also makes concurrent same-output builds atomic.
        try:
            os.link(temporary, output)
        except FileExistsError:
            if output.read_bytes() != temporary.read_bytes():
                raise ValueError(
                    "Different ZIP already exists; bump the extension version"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "version": manifest["version"],
        "files": len(contents),
        "output": str(output),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "vendor/hstry/extension")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = build_extension(args.source, args.output)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Cannot package extension: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
