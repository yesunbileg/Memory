"""Compatibility entry point for ai-memory demo."""

import sys

from ai_memory.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["demo", *sys.argv[1:]]))
