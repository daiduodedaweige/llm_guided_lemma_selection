#!/usr/bin/env python3
"""Create tables and plots for the complete task/method/repeat grid."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reporting import cli

if __name__ == "__main__":
    raise SystemExit(cli())
