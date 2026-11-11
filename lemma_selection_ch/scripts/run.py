#!/usr/bin/env python3
"""Run CH on an already frozen candidate pool."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from experiment import cli
from method import METHOD, run

if __name__ == "__main__":
    raise SystemExit(cli(METHOD, run))
