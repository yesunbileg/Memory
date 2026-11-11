"""Run the project's offline checks from any working directory."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    commands = [
        [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"],
        [sys.executable, "-m", "ruff", "format", "--check", "--quiet", "src", "tests", "scripts"],
        [sys.executable, "-m", "pytest", "-q"],
        ["node", "--test", *map(str, sorted((ROOT / "vendor/hstry/extension/test").glob("*.js")))],
    ]
    for command in commands:
        print(f"Running: {' '.join(command)}", flush=True)
        try:
            result = subprocess.run(command, cwd=ROOT, check=False)
        except FileNotFoundError:
            print(
                "Missing development tools. Run uv sync --frozen and install Node.js 24+.",
                file=sys.stderr,
            )
            return 1
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
