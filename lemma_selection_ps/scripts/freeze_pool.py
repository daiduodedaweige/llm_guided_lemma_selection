#!/usr/bin/env python3
"""Export completed generation records once, then copy the verified frozen package."""
import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pool import copy_pool, export_pool, load_pool, source_records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=PROJECT / "results/gls_20261109_fs1_5samples")
    parser.add_argument("--destination", type=Path, default=PROJECT / "data/frozen_pool/experiment1_v1")
    parser.add_argument("--copy-to", type=Path, nargs="+", default=[
        PROJECT / "lemma_selection_ps/data/frozen_pool/experiment1_v1",
        PROJECT / "lemma_selection_ch/data/frozen_pool/experiment1_v1"])
    parser.add_argument("--copy-only", action="store_true", help="Validate and copy an existing package without re-extraction")
    parser.add_argument("--check-only", action="store_true", help="Audit the source or package without writing any files")
    parser.add_argument("--smoke", action="store_true", help="Export a small acceptance dataset, never a formal 109-task pool")
    args = parser.parse_args(argv)
    try:
        if args.copy_only:
            manifest, _ = load_pool(args.destination)
        else:
            records, provenance = source_records(args.source_root, PROJECT, smoke=args.smoke)
            if args.check_only:
                print(f"Source audit passed: {len(records)} tasks; nothing written")
                return 0
            manifest = export_pool(records, args.destination, provenance=provenance,
                                   scope="smoke" if args.smoke else "formal")
        if not args.check_only:
            for target in args.copy_to:
                if target.resolve() == args.destination.resolve():
                    raise ValueError("Copy destination must differ from the source package")
                copy_pool(args.destination, target)
        print(f"Frozen pool verified: {manifest['pool_hash']}")
        return 0
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"experiment one freeze: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
