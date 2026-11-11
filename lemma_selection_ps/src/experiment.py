"""Frozen-pool experiment runner. No candidate generation and no result cache."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from gls import Config, FormalQueryError, PROMPT_VERSION, Result, Selector
from pool import GROUP_COUNTS, canonical, digest, inside, load_pool, write_json
from runtime import BudgetExhausted, ExperimentBackend, ExperimentEvaluator, RecordedProposer, RunContext
from formal_sva import BACKEND_SEMANTICS_VERSION

METHOD_ROOT = Path(__file__).resolve().parents[1]
PROJECT = METHOD_ROOT.parent
COMMON_FILES = ("evaluation.py", "cache_utils.py", "gls.py", "gls_runtime.py", "backend_compat.py",
                "gls_step_witness.py", "formal_sva.py", "runtime.py", "pool.py", "experiment.py", "reporting.py")


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def check_common():
    hashes = {}
    for name in COMMON_FILES:
        left = PROJECT / "lemma_selection_ps/src" / name
        right = PROJECT / "lemma_selection_ch/src" / name
        if left.read_bytes() != right.read_bytes():
            raise ValueError(f"Common runtime copies differ: {name}")
        hashes[name] = digest(left.read_bytes())
    return digest(canonical(hashes))


def load_profile(path):
    # Reuse the existing read-only config parser without calling its live runner.
    source = PROJECT / "scripts/run_live.py"
    spec = importlib.util.spec_from_file_location("experiment1_live_config", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    profile = module.load_codex_profile(path.expanduser())
    key = profile["key_file"].resolve()
    if not key.is_file() or not key.read_text().strip():
        raise ValueError("Responses key file is missing or empty")
    if key.stat().st_mode & 0o077:
        raise ValueError("Responses key file must have mode 0600")
    from urllib.parse import urlsplit
    url = urlsplit(profile["base_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query:
        raise ValueError("Responses base URL must be HTTPS without embedded credentials")
    profile["key_file"] = key
    return profile


def source_snapshot(method):
    files = {str(p.relative_to(PROJECT)): p for folder in ("src", "scripts")
             for p in sorted((METHOD_ROOT / folder).glob("*.py"))}
    files[str((METHOD_ROOT / "reference_sources.json").relative_to(PROJECT))] = METHOD_ROOT / "reference_sources.json"
    if method == "gls":
        for relative in ("src/gls.py", "scripts/run_live.py"):
            files[relative] = PROJECT / relative
        expected = json.loads((METHOD_ROOT / "reference_sources.json").read_text())["src/gls.py"]
        if digest((PROJECT / "src/gls.py").read_bytes()) != expected:
            raise ValueError("Original GLS source changed since the reference copy; review and refresh the snapshot")
    return files, {name: digest(path.read_bytes()) for name, path in files.items()}


def execute_task(task, pool_dir, directory, *, method, method_run, config, ebmc_path,
                 profile, model_id, effort, repeat, manifest, config_hash, scope):
    directory.mkdir(parents=True, exist_ok=False)
    work = directory / "work"
    work.mkdir()
    rtl = work / (task["name"] + ".sv")
    rtl.write_bytes(inside(pool_dir, task["rtl_path"]).read_bytes())
    pool = {row["id"]: row["text"] for row in task["candidates"]}
    context = RunContext(directory, config.budget_s, config.formal_timeout_s)
    evaluator = ExperimentEvaluator(rtl, ebmc_path, context, pool)
    backend = ExperimentBackend(evaluator, pool)
    selector = None
    if method == "gls":
        proposer = RecordedProposer(profile, model_id, effort, context)
        selector = Selector(rtl.read_text(encoding="utf-8"), task["property_text"],
                            list(pool.values()), backend, proposer, config)
        if selector.pool != pool or selector.pool_hash != task["candidate_pool_hash"]:
            raise ValueError("GLS reconstructed a different candidate pool")
    context.start()
    error = None
    try:
        eligible = evaluator.prepare_candidates()
        backend.pool = eligible
        if selector:
            selector.disabled.update(set(pool) - set(eligible))
        result = selector.run() if selector else method_run(backend, evaluator, context, eligible)
    except FormalQueryError as exc:
        error = exc.as_dict()
        result = Result("ERROR", reason=str(exc), error=error)
    except BudgetExhausted as exc:
        result = Result("TIMEOUT", reason=str(exc))
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        result = selector.result if selector else Result("ERROR")
        result.status, result.reason = "ERROR", str(exc)
    error = error or result.error
    wall = context.elapsed()
    raw_status = result.status
    status = raw_status if raw_status in ("PROVED", "UNSAFE", "TIMEOUT", "UNKNOWN", "ERROR") else "UNKNOWN"
    if status == "UNKNOWN" and pool and len(evaluator.isolated_candidates) == len(pool):
        result.reason = "no executable candidates remain after compiler-error isolation"
        result.reason_code = "no_executable_candidates"
    if wall > config.budget_s and status in ("PROVED", "UNKNOWN"):
        status = "TIMEOUT"  # A late proof or exhausted search cannot count as completed in budget.
    by_phase = defaultdict(float)
    for row in context.queries:
        if row["executed"]:
            by_phase[row["phase"]] += row["elapsed_s"]
    solver_s = sum(by_phase.values())
    llm_s = sum(row["elapsed_s"] for row in context.llm)

    def tokens(field):
        values = [row[field] for row in context.llm]
        return None if any(value is None for value in values) else sum(values)

    generation_s = task["generation_cost"].get("time_s")
    row = {
        "schema_version": 1, "method": method, "repeat": repeat, "run_scope": scope,
        "task_id": task["task_id"], "group": task["group"], "task_name": task["name"],
        "pool_hash": manifest["pool_hash"], "candidate_pool_hash": task["candidate_pool_hash"],
        "rtl_sha256": task["rtl_sha256"], "property_sha256": task["property_sha256"],
        "config_hash": config_hash, "status": status, "raw_status": raw_status,
        "reason": result.reason,
        "reason_code": result.reason_code or (error or {}).get("reason_code") or {"UNKNOWN": "search_inconclusive", "TIMEOUT": "task_budget_or_query_timeout",
                                              "ERROR": "tool_error"}.get(status, ""),
        "backend_semantics_version": BACKEND_SEMANTICS_VERSION,
        "error": error, "direct_status": result.direct_status,
        "selected_ids": list(result.selected_ids) if status == "PROVED" else [],
        "candidate_count": len(pool), "wall_s": wall,
        "isolated_candidates": evaluator.isolated_candidates,
        "eligible_candidate_count": len(pool) - len(evaluator.isolated_candidates),
        "formal_queries": sum(row["executed"] for row in context.queries),
        "formal_queries_definition": "actual EBMC processes, including property discovery",
        "property_discovery_queries": sum(row["executed"] for row in context.queries
                                          if row["kind"] == "property_discovery"),
        "verification_queries": sum(row["executed"] for row in context.queries
                                    if row["kind"] == "verification"),
        "property_discovery_s": sum(row["elapsed_s"] for row in context.queries
                                    if row["executed"] and row["kind"] == "property_discovery"),
        "solver_s": solver_s, "solver_time_definition": "sum of actual EBMC process wall times",
        "query_time_by_phase_s": dict(by_phase), "llm_s": llm_s,
        "other_s": max(0.0, wall - solver_s - llm_s), "llm_calls": len(context.llm),
        "input_tokens": tokens("input_tokens"), "output_tokens": tokens("output_tokens"),
        "selection_queries": result.selection_queries,
        "generation_cost": task["generation_cost"], "generation_s": generation_s,
        "end_to_end_s": None if generation_s is None else generation_s + wall,
        "checked": result.checked, "events": result.events + context.events,
        "random_seed": None,
    }
    write_json(directory / "result.json", row)
    return row


def cli(method, method_run=None, argv=None):
    parser = argparse.ArgumentParser(description=f"Run frozen-pool {method.upper()} independently, without generation")
    default_pool = (PROJECT if method == "gls" else METHOD_ROOT) / "data/frozen_pool/experiment1_v1"
    parser.add_argument("--pool", type=Path, default=default_pool)
    parser.add_argument("--output-root", type=Path, default=PROJECT / "experiment1_results")
    parser.add_argument("--groups", nargs="+", choices=tuple(GROUP_COUNTS), default=list(GROUP_COUNTS))
    parser.add_argument("--tasks", nargs="+", help="Exact group/name task IDs; requires --smoke")
    parser.add_argument("--repeats", nargs="+", type=int, choices=(1, 2, 3), default=[1, 2, 3])
    parser.add_argument("--budget-s", type=positive, default=600.0)
    parser.add_argument("--formal-timeout-s", type=positive, default=120.0)
    parser.add_argument("--ebmc-path", default=os.getenv("EBMC_PATH", "ebmc"))
    parser.add_argument("--smoke", action="store_true", help="Acceptance run, excluded from formal statistics")
    parser.add_argument("--dry-run", action="store_true", help="Validate inputs and show the plan; execute no processes or API requests")
    parser.add_argument("--resume", action="store_true", help="Skip complete results only within the same matching batch")
    if method == "gls":
        parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex/config.toml")
        parser.add_argument("--model-id", default="gpt-6.1-sol")
        parser.add_argument("--reasoning-effort", default="xhigh", choices=("low", "medium", "high", "xhigh"))
    args = parser.parse_args(argv)
    try:
        manifest, tasks = load_pool(args.pool)
        common_hash = check_common()
        sources, source_hashes = source_snapshot(method)
        if manifest["scope"] == "smoke" and not args.smoke:
            raise ValueError("A smoke pool requires --smoke")
        if args.tasks and not args.smoke:
            raise ValueError("Task subsets require --smoke")
        if len(set(args.repeats)) != len(args.repeats) or len(set(args.groups)) != len(args.groups):
            raise ValueError("Duplicate group or repeat")
        if args.tasks and set(args.tasks) - {task["task_id"] for task in tasks}:
            raise ValueError("Unknown task ID")
        tasks = [task for task in tasks if task["group"] in args.groups and
                 (not args.tasks or task["task_id"] in args.tasks)]
        if not tasks:
            raise ValueError("No tasks selected")
        executable = shutil.which(str(args.ebmc_path))
        if executable is None:
            candidate = Path(args.ebmc_path).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                executable = str(candidate.resolve())
        config = Config(budget_s=args.budget_s, formal_timeout_s=args.formal_timeout_s)
        plan = {"method": method, "pool_hash": manifest["pool_hash"],
                "backend_semantics_version": BACKEND_SEMANTICS_VERSION,
                "tasks": [t["task_id"] for t in tasks], "repeats": args.repeats,
                "task_runs": len(tasks) * len(args.repeats), "config": asdict(config),
                "common_source_hash": common_hash, "ebmc_path": executable,
                "run_scope": "smoke" if args.smoke else "formal"}
        if args.dry_run:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        if executable is None:
            raise ValueError("EBMC executable not found; supply --ebmc-path")
        args.pool = args.pool.resolve()
        output = args.output_root.resolve()
        if output == args.pool or output.is_relative_to(args.pool):
            raise ValueError("Results must not be written into the frozen pool")
        profile = load_profile(args.codex_config) if method == "gls" else None
        version = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                 errors="replace", timeout=10)
        if version.returncode != 0 or not (version.stdout + version.stderr).strip():
            raise ValueError("Cannot read EBMC version")
        metadata = {**plan, "source_hashes": source_hashes,
                    "ebmc_sha256": digest(Path(executable).read_bytes()),
                    "ebmc_version": (version.stdout + version.stderr).strip(),
                    "model_id": getattr(args, "model_id", None),
                    "reasoning_effort": getattr(args, "reasoning_effort", None),
                    "prompt_version": PROMPT_VERSION if method == "gls" else None,
                    "base_url": profile["base_url"] if profile else None,
                    "cache_policy": "disabled", "concurrency": 1,
                    "python": sys.version, "platform": platform.platform(),
                    "machine": platform.node(), "cpu_count": os.cpu_count(),
                    "processor": platform.processor()}
        config_hash = digest(canonical({key: value for key, value in metadata.items()
                                        if key not in ("tasks", "repeats")}))
        batches = []
        for repeat in args.repeats:
            for group in args.groups:
                selected = [task for task in tasks if task["group"] == group]
                if not selected:
                    continue
                directory = output / method / f"repeat_{repeat}" / group
                old_manifest = directory / "run.json"
                if old_manifest.exists():
                    old = json.loads(old_manifest.read_text())
                    if not args.resume or old["config_hash"] != config_hash or old["task_ids"] != [t["task_id"] for t in selected]:
                        raise ValueError(f"Existing batch differs or --resume missing: {directory}")
                elif directory.exists() and any(directory.iterdir()):
                    raise ValueError(f"Unrecognized nonempty batch directory: {directory}")
                batches.append((repeat, selected, directory))
        failures = 0
        for repeat, selected, directory in batches:
            batch = {**metadata, "repeat": repeat, "config_hash": config_hash,
                     "task_ids": [t["task_id"] for t in selected], "status": "running",
                     "started_at_utc": datetime.now(timezone.utc).isoformat()}
            write_json(directory / "run.json", batch)
            for name, source in sources.items():
                path = directory / "source_snapshot" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source.read_bytes())
            for task in selected:
                task_dir = directory / task["name"]
                existing = task_dir / "result.json"
                if existing.exists():
                    row = json.loads(existing.read_text())
                    if row["config_hash"] != config_hash or row["pool_hash"] != manifest["pool_hash"]:
                        raise ValueError("Existing task result identity mismatch")
                else:
                    if task_dir.exists():
                        shutil.rmtree(task_dir)
                    row = execute_task(task, args.pool, task_dir, method=method, method_run=method_run,
                                       config=config, ebmc_path=executable, profile=profile,
                                       model_id=getattr(args, "model_id", None), effort=getattr(args, "reasoning_effort", None),
                                       repeat=repeat, manifest=manifest, config_hash=config_hash,
                                       scope=plan["run_scope"])
                failures += row["status"] == "ERROR"
                print(f"{method} repeat={repeat} {task['task_id']}: {row['status']} "
                      f"{row['wall_s']:.3f}s {row['formal_queries']} queries", flush=True)
            batch["status"] = "completed"
            batch["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            write_json(directory / "run.json", batch)
        return 1 if failures else 0
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as exc:
        parser.exit(2, f"experiment one: {exc}\n")
