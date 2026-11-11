"""Build a load-unpacked ZIP containing the extension's public runtime files."""

import argparse
import hashlib
import json
import posixpath
import re
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


def validate_runtime(manifest: dict, files: dict[str, Path]):
    if manifest.get("manifest_version") != 3:
        raise ValueError("Expected a Manifest V3 extension")
    references = [
        ("manifest.json", manifest["options_page"]),
        ("manifest.json", manifest["background"]["service_worker"]),
        ("manifest.json", manifest["action"]["default_popup"]),
    ]
    icons = manifest.get("icons", {})
    action_icons = manifest.get("action", {}).get("default_icon", {})
    references.extend(("manifest.json", path) for path in icons.values())
    references.extend(
        ("manifest.json", path)
        for path in (action_icons.values() if isinstance(action_icons, dict) else [action_icons])
    )
    for name, path in files.items():
        pattern = (
            JS_IMPORTS if path.suffix == ".js" else HTML_ASSETS if path.suffix == ".html" else None
        )
        if pattern:
            references.extend(
                (name, ref) for ref in pattern.findall(path.read_text(encoding="utf-8"))
            )
    for origin, reference in references:
        if reference.startswith(("https://", "http://", "data:")):
            continue
        reference = reference.split("?", 1)[0].split("#", 1)[0]
        target = posixpath.normpath(posixpath.join(posixpath.dirname(origin), reference))
        if target not in files:
            raise ValueError(f"Missing runtime file {reference!r} referenced by {origin}")


def build_extension(source: Path, output: Path | None = None) -> dict:
    source = source.expanduser().resolve(strict=True)
    files = {
        p.relative_to(source).as_posix(): p
        for pattern in PATTERNS
        for p in source.glob(pattern)
        if p.is_file()
    }
    for name, path in files.items():
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError(f"Runtime files must stay inside the extension: {name}")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    validate_runtime(manifest, files)
    contents = {name: path.read_bytes() for name, path in files.items()}
    license_path = source.parent / "LICENSE"
    if license_path.is_symlink():
        raise ValueError("The extension license must be a regular file")
    contents["LICENSE"] = license_path.read_bytes()
    output = (
        (output or ROOT / "dist" / f"ai-memory-extension-{manifest['version']}.zip")
        .expanduser()
        .resolve()
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name, content in sorted(contents.items()):
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        temporary.replace(output)
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
