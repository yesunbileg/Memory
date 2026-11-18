"""Record the precise deliverables from one clean checkout and build run."""

import argparse
import hashlib
import json
import subprocess
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def describe(path: Path) -> dict:
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            files = [item for item in archive.getmembers() if item.isfile()]
            metadata = next(
                item
                for item in files
                if item.name.count("/") == 1 and item.name.endswith("/PKG-INFO")
            )
            version = BytesParser().parsebytes(archive.extractfile(metadata).read())["Version"]
    else:
        with zipfile.ZipFile(path) as archive:
            files = [name for name in archive.namelist() if not name.endswith("/")]
            if path.suffix == ".whl":
                entry = next(name for name in files if name.endswith(".dist-info/METADATA"))
                version = BytesParser().parsebytes(archive.read(entry))["Version"]
            else:
                version = json.loads(archive.read("manifest.json"))["version"]
    return {
        "name": path.name,
        "files": len(files),
        "version": str(version),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def check_version(previous: Path, current: Path):
    old, new = describe(previous), describe(current)

    def parts(version):
        values = [int(part) for part in version.split(".")]
        return tuple(values + [0] * (4 - len(values)))

    if new["sha256"] != old["sha256"] and parts(new["version"]) <= parts(old["version"]):
        raise ValueError("Different extension ZIP bytes require a higher release version")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--previous-extension", type=Path)
    args = parser.parse_args()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT
    )
    if dirty:
        parser.exit(1, "Release verification requires a clean checkout.\n")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    build = args.dist
    paths = []
    for pattern in ("*.whl", "*.tar.gz", "*.zip"):
        choices = list(build.glob(pattern))
        if len(choices) != 1:
            parser.exit(1, f"Expected exactly one {pattern} artifact in dist.\n")
        paths.append(choices[0])
    if args.previous_extension:
        check_version(args.previous_extension, paths[-1])
    print(json.dumps({"commit": commit, "artifacts": [describe(p) for p in paths]}, indent=2))


if __name__ == "__main__":
    main()
