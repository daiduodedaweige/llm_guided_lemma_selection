#!/usr/bin/env python3
"""Call the unchanged original GLS selector with the audited frozen-pool runtime."""
import importlib.util
import sys
from pathlib import Path

sys.dont_write_bytecode = True
METHOD_ROOT = Path(__file__).resolve().parents[1]
PROJECT = METHOD_ROOT.parent
sys.path.insert(0, str(METHOD_ROOT / "src"))
spec = importlib.util.spec_from_file_location("gls", PROJECT / "src/gls.py")
module = importlib.util.module_from_spec(spec)
sys.modules["gls"] = module
spec.loader.exec_module(module)

from experiment import cli

if __name__ == "__main__":
    raise SystemExit(cli("gls"))
