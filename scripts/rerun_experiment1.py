#!/usr/bin/env python3
"""Rerun one method's ERROR records and publish each complete result."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
SUMMARY_FILES = ("runs.json", "runs.csv", "comparison.csv", "metrics.csv",
                 "summary.json", "paired_ch_gls.csv")
LINE = re.compile(r"^gls repeat=([123]) ([A-Za-z0-9_.+/-]+): (\w+) ([0-9.]+)s (\d+) queries$")
CH_LINE = re.compile(r"^ch repeat=([123]) ([A-Za-z0-9_.+/-]+): (\w+) ([0-9.]+)s (\d+) queries$")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    os.replace(temporary, path)


def now():
    return datetime.now(timezone.utc).isoformat()


def select_errors(log, method="gls", *, task_ids=None, repeats=None):
    pattern = LINE if method == "gls" else CH_LINE
    targets, seen = [], set()
    for line in log.splitlines():
        match = pattern.fullmatch(line)
        if match and match[3] == "ERROR":
            repeat, task_id = int(match[1]), match[2]
            if task_id.count("/") != 1 or any(part in (".", "..") for part in task_id.split("/")):
                raise ValueError(f"Invalid task ID: {task_id}")
            key = f"{method}/{repeat}/{task_id}"
            if key in seen:
                raise ValueError(f"Duplicate log record: {key}")
            seen.add(key)
            targets.append({"key": key, "repeat": repeat, "task_id": task_id, "phase": "pending"})
    return [entry for entry in targets
            if (task_ids is None or entry["task_id"] in task_ids)
            and (repeats is None or entry["repeat"] in repeats)]


def update_log(text, row):
    pattern = LINE if row["method"] == "gls" else CH_LINE
    lines, matches = text.splitlines(keepends=True), []
    for index, line in enumerate(lines):
        match = pattern.fullmatch(line.rstrip("\r\n"))
        if match and int(match[1]) == row["repeat"] and match[2] == row["task_id"]:
            matches.append(index)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one log entry for {row['task_id']}, repeat {row['repeat']}")
    lines[matches[0]] = (f"{row['method']} repeat={row['repeat']} {row['task_id']}: {row['status']} "
                        f"{row['wall_s']:.3f}s {row['formal_queries']} queries\n")
    return "".join(lines)


def validate_task(folder, task, repeat, manifest, method="gls"):
    row = read(folder / "result.json")
    expected = {"method": method, "repeat": repeat, "task_id": task["task_id"],
                "group": task["group"], "task_name": task["name"], "run_scope": "formal",
                "pool_hash": manifest["pool_hash"], "candidate_pool_hash": task["candidate_pool_hash"],
                "rtl_sha256": task["rtl_sha256"], "property_sha256": task["property_sha256"]}
    if any(row.get(k) != v for k, v in expected.items()):
        raise ValueError("Rerun identity/input mismatch")
    if row["status"] not in ("PROVED", "UNSAFE", "UNKNOWN", "TIMEOUT", "ERROR"):
        raise ValueError("Invalid result status")
    for name in ("wall_s", "solver_s", "llm_s", "formal_queries", "llm_calls"):
        if not isinstance(row[name], (int, float)) or not math.isfinite(row[name]) or row[name] < 0:
            raise ValueError(f"Invalid metric: {name}")
    if not set(row["selected_ids"]) <= {c["id"] for c in task["candidates"]}:
        raise ValueError("Selected candidate outside frozen pool")
    query_path = folder / "formal_queries.jsonl"
    queries = [json.loads(line) for line in query_path.read_text().splitlines()] if query_path.exists() else []
    if sum(bool(q["executed"]) for q in queries) != row["formal_queries"]:
        raise ValueError("Query count does not match result")
    if not math.isclose(sum(q["elapsed_s"] for q in queries if q["executed"]), row["solver_s"], abs_tol=1e-7):
        raise ValueError("EBMC process times do not match result")
    expected_dirs = {f"query_{index:04d}" for index in range(1, len(queries) + 1)}
    query_root = folder / "formal_logs"
    actual_dirs = {p.name for p in query_root.iterdir() if p.is_dir()} if query_root.exists() else set()
    if actual_dirs != expected_dirs:
        raise ValueError("Query directories are missing or stale")
    for index, query in enumerate(queries, 1):
        qdir = query_root / f"query_{index:04d}"
        if query["index"] != index or read(qdir / "query.json") != query:
            raise ValueError("Query journal mismatch")
        if not all((qdir / name).is_file() for name in ("input.sv", "stdout.log", "stderr.log")):
            raise ValueError("Incomplete formal artifacts")
    model_root = folder / "llm_logs"
    requests = list(model_root.glob("request_*/request.json")) if model_root.exists() else []
    if len(requests) != row["llm_calls"]:
        raise ValueError("LLM request count mismatch")
    return row


def export_summary(output, rows, manifest):
    from reporting import METHODS, METRICS, STATUSES, summarize
    from pool import GROUP_COUNTS
    rows = [dict(row, final_lemma_count=len(row.get("selected_ids", [])) if row["status"] == "PROVED" else None)
            for row in rows]
    summary, paired = summarize(rows, METHODS, [1, 2, 3])
    output.mkdir(parents=True, exist_ok=True)
    write(output / "runs.json", rows)
    write(output / "summary.json", {
        "schema_version": 1, "pool_hash": manifest["pool_hash"], "run_scope": "formal",
        "groups": summary, "paired_ch_gls": paired,
        "notes": ["Standard deviation is the sample standard deviation.",
                  "Success counts are reported per repeat; no virtual-best union is used.",
                  "Missing runs remain in the denominator and have no invented time/query values.",
                  "Solver Time is EBMC process wall time, including frontend work.",
                  "Generation costs cover recorded responses; unavailable historical retry costs remain missing."]})
    with (output / "runs.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["method", "repeat", "group", "task_id", "status", *METRICS], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with (output / "comparison.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["group", "method", "repeat", "total", *STATUSES, "success_rate"])
        for group in (*GROUP_COUNTS, "all"):
            for method, data in summary[group].items():
                for row in data["per_repeat"]:
                    writer.writerow([group, method, row["repeat"], row["total"],
                                     *[row["statuses"][s] for s in STATUSES], row["success_rate"]])
    with (output / "metrics.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["group", "method", "sample_scope", "metric", "n", "missing", "mean", "std", "median"])
        for group, methods in summary.items():
            for method, data in methods.items():
                for scope in ("metrics_proved", "metrics_all_observed"):
                    for metric, values in data[scope].items():
                        writer.writerow([group, method, scope, metric,
                                         *[values[k] for k in ("n", "missing", "mean", "std", "median")]])
    with (output / "paired_ch_gls.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["repeat", "task_id", "group", "speedup_ch_over_gls", "query_difference_ch_minus_gls"])
        writer.writeheader()
        writer.writerows(paired)


def bootstrap(method="gls"):
    sys.path.insert(0, str(PROJECT / ("lemma_selection_ps/src" if method == "gls" else "lemma_selection_ch/src")))
    if method == "gls":
        spec = importlib.util.spec_from_file_location("gls", PROJECT / "src/gls.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["gls"] = module
        spec.loader.exec_module(module)


def collect_current(pool, results):
    from reporting import METHODS, collect
    manifest, tasks, rows = collect(pool, results, METHODS, [1, 2, 3], "formal")
    if len(rows) != len(tasks) * 9 or any(r["status"] == "MISSING" for r in rows):
        raise ValueError("Expected a complete original result grid")
    return manifest, tasks, rows


def rollback(results, operation, entry):
    transaction = entry.get("transaction")
    if not transaction:
        return
    previous = operation / transaction["previous_task"]
    target = results / entry["relative"]
    if previous.exists():
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(previous), str(target))
    for relative, exists in transaction["files"].items():
        dest = results / relative
        if exists:
            shutil.copy2(operation / transaction["backup"] / relative, dest)
        elif dest.exists():
            dest.unlink()
    entry.pop("transaction", None)
    entry["phase"] = "ready"


def publish(results, pool, operation, state, entry, task, manifest):
    from pool import canonical, digest
    folder = operation / "executions" / entry["relative"]
    method = entry.get("method", "gls")
    row = validate_task(folder, task, entry["repeat"], manifest, method)
    _, _, rows = collect_current(pool, results)
    position = next(i for i, r in enumerate(rows) if
                    (r["method"], r["repeat"], r["task_id"]) == (method, entry["repeat"], entry["task_id"]))
    current = results / entry["relative"]
    if sha(current / "result.json") != entry["original_sha256"]:
        raise ValueError("Original result changed during rerun")
    rows[position] = row
    pending = operation / "pending" / entry["relative"]
    export_summary(pending / "summary", rows, manifest)
    log = results / f"logs/{method}.log"
    (pending / f"{method}.log").write_text(update_log(log.read_text(), row))
    index_path = results / "logs/rerun_index.json"
    index = read(index_path) if index_path.exists() else {"schema_version": 1, "records": {}}
    batch = read(current.parent / "run.json")
    index["records"][entry["key"]] = {
        "operation": str(operation.relative_to(results)), "result_sha256": sha(folder / "result.json"),
        "previous_result_sha256": entry["original_sha256"], "config_hash": row["config_hash"],
        "batch_config_hash": batch["config_hash"]}
    write(pending / "rerun_index.json", index)
    backup = operation / "rollback" / entry["relative"]
    relative_files = [f"summary/{name}" for name in SUMMARY_FILES] + [f"logs/{method}.log", "logs/rerun_index.json"]
    exists = {}
    for relative in relative_files:
        source = results / relative
        exists[relative] = source.exists()
        if source.exists():
            dest = backup / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    previous = operation / "previous" / entry["relative"]
    entry["transaction"] = {"backup": str(backup.relative_to(operation)),
                            "previous_task": str(previous.relative_to(operation)), "files": exists}
    entry["phase"] = "publishing"
    write(operation / "operation.json", state)
    try:
        replacement = current.with_name("." + current.name + ".rerun")
        if replacement.exists():
            shutil.rmtree(replacement)
        shutil.copytree(folder, replacement)
        previous.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(current), str(previous))
        os.replace(replacement, current)
        for relative in relative_files:
            source = pending / (relative.removeprefix("summary/") if relative.startswith("summary/") else Path(relative).name)
            if relative.startswith("summary/"):
                source = pending / relative
            os.replace(source, results / relative)
        _, _, actual = collect_current(pool, results)
        effective = next(r for r in actual if
                         (r["method"], r["repeat"], r["task_id"]) == (method, entry["repeat"], entry["task_id"]))
        if effective["status"] != row["status"] or sha(current / "result.json") != sha(folder / "result.json"):
            raise ValueError("Published result mismatch")
        entry.update(phase="published", status=row["status"], wall_s=row["wall_s"],
                     formal_queries=row["formal_queries"], completed_at_utc=now())
        entry.pop("transaction", None)
        write(operation / "operation.json", state)
    except BaseException:
        rollback(results, operation, entry)
        write(operation / "operation.json", state)
        raise
    print(f"PUBLISHED {entry['key']}: {row['status']} {row['wall_s']:.3f}s {row['formal_queries']} queries", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=("gls", "ch"), default="gls")
    parser.add_argument("--results", type=Path, default=PROJECT / "experiment1_results")
    parser.add_argument("--pool", type=Path, default=PROJECT / "data/frozen_pool/experiment1_v1")
    parser.add_argument("--operation", type=Path, required=True)
    parser.add_argument("--tasks", nargs="+", metavar="GROUP/NAME",
                        help="Restrict reruns to these tasks' ERROR records")
    parser.add_argument("--repeats", nargs="+", type=int, choices=(1, 2, 3),
                        help="Restrict reruns to these repeat numbers")
    parser.add_argument("--reason", default="Retry ERROR records with the current backend",
                        help="Reason recorded for this rerun operation")
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    results, pool, operation = args.results.resolve(), args.pool.resolve(), args.operation.resolve()
    if not operation.is_relative_to(results / "logs/reruns") or operation == results / "logs/reruns":
        raise ValueError("Operation must be a new child of results/logs/reruns")
    bootstrap(args.method)
    from experiment import Config, check_common, execute_task, load_profile, source_snapshot
    from pool import canonical, digest
    from formal_sva import BACKEND_SEMANTICS_VERSION
    with (results / "logs/rerun.lock").open("a") as lock:
        if args.method == "ch":
            print("WAITING for GLS result lock before CH rerun", flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
            print("ACQUIRED result lock; starting CH rerun", flush=True)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = operation / "operation.json"
        if state_path.exists():
            if not args.resume:
                raise ValueError("Operation exists; use --resume")
            state = read(state_path)
            if state["results"] != str(results) or state["pool"] != str(pool) or state.get("method", "gls") != args.method:
                raise ValueError("Resume paths differ")
            if args.tasks is not None and set(args.tasks) != {entry["task_id"] for entry in state["targets"]}:
                raise ValueError("Resume task selection differs")
            if args.repeats is not None and set(args.repeats) != {entry["repeat"] for entry in state["targets"]}:
                raise ValueError("Resume repeat selection differs")
            for entry in state["targets"]:
                if entry["phase"] == "publishing":
                    rollback(results, operation, entry)
            write(state_path, state)
        else:
            targets = select_errors((results / f"logs/{args.method}.log").read_text(), args.method,
                                    task_ids=args.tasks, repeats=args.repeats)
            if not targets:
                raise ValueError(f"No {args.method.upper()} ERROR records in log")
            if args.tasks is not None:
                requested = {(repeat, task_id) for task_id in args.tasks
                             for repeat in (args.repeats or (1, 2, 3))}
                selected = {(entry["repeat"], entry["task_id"]) for entry in targets}
                if requested != selected:
                    raise ValueError(f"Requested ERROR records not found: {sorted(requested - selected)}")
            state = {"schema_version": 1, "status": "prepared", "method": args.method, "results": str(results),
                     "pool": str(pool), "started_at_utc": now(), "targets": targets,
                     "reason": args.reason}
        manifest, tasks, original_rows = collect_current(pool, results)
        task_map = {t["task_id"]: t for t in tasks}
        for entry in state["targets"]:
            if entry["task_id"] not in task_map:
                raise ValueError("Unknown task")
            entry["method"] = args.method
            entry["relative"] = f"{args.method}/repeat_{entry['repeat']}/{entry['task_id']}"
            if "original_sha256" not in entry:
                path = results / entry["relative"] / "result.json"
                if read(path)["status"] != "ERROR":
                    raise ValueError("Log ERROR does not match task result")
                entry["original_sha256"] = sha(path)
        if args.dry_run:
            print(json.dumps({"runs": len(state["targets"]), "properties": len({e['task_id'] for e in state['targets']}),
                              "targets": [e["key"] for e in state["targets"]]}, indent=2))
            return 0
        common_hash = check_common()
        _, source_hashes = source_snapshot(args.method)
        profile = load_profile(args.codex_config) if args.method == "gls" else None
        if args.method == "ch":
            from method import run as method_run
        else:
            method_run = None
        if not state_path.exists():
            operation.mkdir(parents=True, exist_ok=False)
            selected = {e["relative"] for e in state["targets"]}
            protected = [results / r["method"] / f"repeat_{r['repeat']}" / r["task_id"] / "result.json"
                         for r in original_rows if f"{r['method']}/repeat_{r['repeat']}/{r['task_id']}" not in selected]
            protected += list((results / "summary").glob("*.png")) + list((results / "summary").glob("*.pdf"))
            protected += list(results.glob("*/repeat_*/*/run.json"))
            protected += [p for p in (results / "logs").glob("*") if p.is_file() and p.name in
                          ({"ps.log", "ch.log", "gls.log", "gls_exit.json", "ps_exit.json", "ch_exit.json"} - {f"{args.method}.log"})]
            report = PROJECT / "ex1_data/experiment1_paper_results.md"
            if report.exists():
                protected.append(report)
            state["protected"] = {str(p): sha(p) for p in protected}
            initial = operation / "initial"
            for relative in [f"logs/{args.method}.log", "logs/rerun_index.json", *[f"summary/{n}" for n in SUMMARY_FILES]]:
                path = results / relative
                if path.exists():
                    dest = initial / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)
            write(state_path, state)
        state["status"] = "running"
        write(state_path, state)
        for number, entry in enumerate(state["targets"], 1):
            if entry["phase"] == "published":
                continue
            task = task_map[entry["task_id"]]
            current = results / entry["relative"]
            batch = read(current.parent / "run.json")
            config = Config(**batch["config"])
            folder = operation / "executions" / entry["relative"]
            print(f"START {number}/{len(state['targets'])} {entry['key']} {now()}", flush=True)
            if not (folder / "result.json").exists():
                if folder.exists():
                    incomplete = folder.with_name(folder.name + ".incomplete." + datetime.now().strftime("%H%M%S"))
                    shutil.move(str(folder), str(incomplete))
                entry["phase"] = "running"
                write(state_path, state)
                config_hash = digest(canonical({"config": batch["config"], "source_hashes": source_hashes,
                                                "common_source_hash": common_hash,
                                                "backend_semantics_version": BACKEND_SEMANTICS_VERSION,
                                                "model_id": batch["model_id"], "reasoning_effort": batch["reasoning_effort"]}))
                execute_task(task, pool, folder, method=args.method, method_run=method_run, config=config,
                             ebmc_path=batch["ebmc_path"], profile=profile, model_id=batch["model_id"],
                             effort=batch["reasoning_effort"], repeat=entry["repeat"], manifest=manifest,
                             config_hash=config_hash, scope="formal")
            validate_task(folder, task, entry["repeat"], manifest, args.method)
            entry["phase"] = "ready"
            write(state_path, state)
            publish(results, pool, operation, state, entry, task, manifest)
        for name, expected in state["protected"].items():
            if sha(Path(name)) != expected:
                raise ValueError(f"Protected original artifact changed: {name}")
        _, _, final_rows = collect_current(pool, results)
        verify = operation / "verification_summary"
        export_summary(verify, final_rows, manifest)
        for name in SUMMARY_FILES:
            if sha(verify / name) != sha(results / "summary" / name):
                raise ValueError(f"Final summary mismatch: {name}")
        state.update(status="completed", finished_at_utc=now(),
                     outcomes=dict(Counter(e["status"] for e in state["targets"])),
                     verified_result_count=len(final_rows), protected_files_verified=len(state["protected"]))
        write(state_path, state)
        print("COMPLETED " + json.dumps(state["outcomes"], sort_keys=True), flush=True)
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise SystemExit(2) from exc
