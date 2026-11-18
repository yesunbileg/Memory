"""Check actual wheel-from-sdist contents against public runtime inputs."""

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_python_packages_contain_adapters_not_unrelated_workstation_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md", "README.md", ".gitignore"):
        shutil.copyfile(ROOT / name, source / name)
    shutil.copytree(ROOT / "src", source / "src")
    shutil.copytree(ROOT / "vendor/hstry", source / "vendor/hstry")
    marker = "PRIVATE_PACKAGING_FIXTURE"
    for name in (
        "untracked-notes.md",
        "db.sqlite.files/exports/transcript.md",
        "src/ai_memory/workstation.log",
        "vendor/hstry/adapters/codex/local-debug.ts",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(marker)
    dist = tmp_path / "dist"
    # uv sync already cached the build backend for the editable development install.
    subprocess.run(["uv", "build", "--offline", "--out-dir", str(dist)], cwd=source, check=True)
    with tarfile.open(next(dist.glob("*.tar.gz"))) as archive:
        files = {name.partition("/")[2] for name in archive.getnames()}
        assert {"pyproject.toml", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"} <= files
        for name in archive.getnames():
            entry = archive.getmember(name)
            if entry.isfile():
                assert marker.encode() not in archive.extractfile(entry).read()
    with zipfile.ZipFile(next(dist.glob("*.whl"))) as wheel:
        names = set(wheel.namelist())
        assert "ai_memory/cli.py" in names
        assert any(n.endswith(".dist-info/licenses/LICENSE") for n in names)
        for name in (
            "adapters/codex/adapter.ts",
            "adapters/opencode/adapter.ts",
            "adapters/types/index.ts",
            "adapters/types/first-message.ts",
            "adapters/types/package.json",
            "LICENSE",
            "UPSTREAM.json",
            "PATCHES.md",
        ):
            assert (
                wheel.read(f"ai_memory/vendor/hstry/{name}")
                == (ROOT / "vendor/hstry" / name).read_bytes()
            )
        assert all(marker.encode() not in wheel.read(n) for n in names)
