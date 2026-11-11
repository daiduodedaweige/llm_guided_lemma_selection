"""Paired, repeat-aware experiment-one summaries and standalone figures."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
from collections import Counter
from pathlib import Path

from pool import GROUP_COUNTS, digest, load_pool, write_json

METHODS = ("ps", "ch", "gls")
VALID_RESULT_STATUSES = ("PROVED", "UNSAFE", "TIMEOUT", "UNKNOWN", "ERROR")
STATUSES = ("PROVED", "TIMEOUT", "UNKNOWN", "ERROR", "MISSING")
METRICS = ("wall_s", "formal_queries", "solver_s", "llm_s", "llm_calls",
           "input_tokens", "output_tokens", "final_lemma_count", "generation_s", "end_to_end_s")


def stats(values):
    observed = [value for value in values if value is not None]
    return {"n": len(observed), "missing": len(values) - len(observed),
            "mean": statistics.mean(observed) if observed else None,
            "std": statistics.stdev(observed) if len(observed) > 1 else None,
            "median": statistics.median(observed) if observed else None}


def collect(pool_dir, results, methods, repeats, scope):
    manifest, tasks = load_pool(pool_dir)
    rows, environments = [], []
    replacement_path = results / "logs" / "rerun_index.json"
    replacements = json.loads(replacement_path.read_text()).get("records", {}) if replacement_path.exists() else {}
    if manifest["scope"] == "smoke" and scope != "smoke":
        raise ValueError("Smoke pool requires --smoke summary")
    for method in methods:
        for repeat in repeats:
            for task in tasks:
                directory = results / method / f"repeat_{repeat}" / task["group"]
                result_path = directory / task["name"] / "result.json"
                identity = {"method": method, "repeat": repeat, "task_id": task["task_id"],
                            "group": task["group"], "task_name": task["name"]}
                if not result_path.exists():
                    rows.append({**identity, "status": "MISSING"})
                    continue
                row = json.loads(result_path.read_text(encoding="utf-8"))
                batch = json.loads((directory / "run.json").read_text(encoding="utf-8"))
                replacement = replacements.get(f"{method}/{repeat}/{task['task_id']}")
                if replacement and (
                    replacement["result_sha256"] != digest(result_path.read_bytes()) or
                    replacement["config_hash"] != row["config_hash"] or
                    replacement["batch_config_hash"] != batch["config_hash"]
                ):
                    raise ValueError(f"Rerun replacement identity mismatch: {result_path}")
                if any(row.get(key) != value for key, value in identity.items()):
                    raise ValueError(f"Result identity mismatch: {result_path}")
                if row["run_scope"] != scope:
                    raise ValueError(f"Result scope differs from summary scope: {result_path}")
                if (row["pool_hash"] != manifest["pool_hash"] or
                    row["candidate_pool_hash"] != task["candidate_pool_hash"] or
                    row["rtl_sha256"] != task["rtl_sha256"] or
                    row["property_sha256"] != task["property_sha256"] or
                    (batch["config_hash"] != row["config_hash"] and replacement is None) or
                    batch["pool_hash"] != manifest["pool_hash"]):
                    raise ValueError(f"Input/configuration hash mismatch: {result_path}")
                if row["status"] not in VALID_RESULT_STATUSES:
                    raise ValueError(f"Invalid final status: {result_path}")
                known_ids = {candidate["id"] for candidate in task["candidates"]}
                if not set(row["selected_ids"]).issubset(known_ids):
                    raise ValueError("Selected ID is outside the frozen pool")
                for field in METRICS:
                    value = row.get(field)
                    if value is not None and (not isinstance(value, (int, float)) or
                                               not math.isfinite(value) or value < 0):
                        raise ValueError(f"Invalid numeric metric {field}: {result_path}")
                if row["status"] == "PROVED" and row["wall_s"] > batch["config"]["budget_s"]:
                    raise ValueError("An over-budget proof was reported as PROVED")
                row["final_lemma_count"] = len(row["selected_ids"]) if row["status"] == "PROVED" else None
                rows.append(row)
                environments.append((batch["common_source_hash"], batch["ebmc_sha256"],
                                     batch["ebmc_version"], batch["config"]["budget_s"],
                                     batch["config"]["formal_timeout_s"], batch["concurrency"],
                                     batch["machine"], batch["platform"], batch["python"]))
    if len(set(environments)) > 1:
        raise ValueError("Runs used different common sources, EBMC binaries, budgets or concurrency")
    for method in methods:
        batches = []
        for repeat in repeats:
            for group in GROUP_COUNTS:
                path = results / method / f"repeat_{repeat}" / group / "run.json"
                if path.exists():
                    batch = json.loads(path.read_text())
                    batches.append(batch["config_hash"])
        if len(set(batches)) > 1:
            raise ValueError(f"{method}: configuration/source/model changed across repeats or groups")
    return manifest, tasks, rows


def summarize(rows, methods, repeats):
    output = {}
    for group in (*GROUP_COUNTS, "all"):
        output[group] = {}
        for method in methods:
            selected = [row for row in rows if row["method"] == method and
                        (group == "all" or row["group"] == group)]
            per_repeat = []
            for repeat in repeats:
                subset = [row for row in selected if row["repeat"] == repeat]
                counts = Counter(row["status"] for row in subset)
                per_repeat.append({"repeat": repeat, "total": len(subset),
                                   "statuses": {status: counts[status] for status in STATUSES},
                                   "success_rate": counts["PROVED"] / len(subset) if subset else None})
            observed = [row for row in selected if row["status"] != "MISSING"]
            proved = [row for row in selected if row["status"] == "PROVED"]
            output[group][method] = {
                "per_repeat": per_repeat,
                "unresolved_reasons": dict(Counter(
                    (row.get("reason_code") or row.get("reason") or "unspecified")
                    for row in observed if row["status"] in ("UNKNOWN", "TIMEOUT", "ERROR"))),
                "proved_per_repeat": stats([row["statuses"]["PROVED"] for row in per_repeat]),
                "metrics_proved": {field: stats([row.get(field) for row in proved]) for field in METRICS},
                "metrics_all_observed": {field: stats([row.get(field) for row in observed]) for field in METRICS},
            }
    paired = []
    indexed = {(row["method"], row["repeat"], row["task_id"]): row for row in rows}
    for (method, repeat, task_id), row in indexed.items():
        other = indexed.get(("ch", repeat, task_id))
        if method == "gls" and row["status"] == "PROVED" and other and other["status"] == "PROVED":
            paired.append({"repeat": repeat, "task_id": task_id, "group": row["group"],
                           "speedup_ch_over_gls": other["wall_s"] / row["wall_s"] if row["wall_s"] > 0 else None,
                           "query_difference_ch_minus_gls": other["formal_queries"] - row["formal_queries"]})
    return output, paired


def figures(directory, rows, methods, repeats, paired):
    os.environ.setdefault("MPLCONFIGDIR", str((directory / ".matplotlib").resolve()))
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    for group in (*GROUP_COUNTS, "all"):
        selected = [row for row in rows if group == "all" or row["group"] == group]
        if not selected:
            continue
        fig, ax = plt.subplots(figsize=(7, 4.5))
        for method in methods:
            for repeat in repeats:
                subset = [row for row in selected if row["method"] == method and row["repeat"] == repeat]
                times = sorted(row["wall_s"] for row in subset if row["status"] == "PROVED")
                line, = ax.step(range(1, len(times) + 1), times, where="post",
                                label=f"{method} r{repeat}: {len(times)}/{len(subset)}")
                if len(times) < len(subset):
                    ax.plot([len(times)], [times[-1] if times else 0], marker="x", color=line.get_color())
        total = max((sum(row["method"] == method and row["repeat"] == repeat for row in selected)
                     for method in methods for repeat in repeats), default=0)
        ax.set(xlabel="Number of proved tasks", ylabel="Proof Time (s)", xlim=(0, max(1, total)),
               title=f"{group}: all tasks retained; x marks an incomplete curve")
        ax.legend(fontsize=7)
        fig.tight_layout()
        for suffix in ("png", "pdf"):
            fig.savefig(directory / f"cactus_{group}.{suffix}")
        plt.close(fig)

        pairs = [row for row in paired if (group == "all" or row["group"] == group)
                 and row["speedup_ch_over_gls"] is not None]
        fig, ax = plt.subplots(figsize=(7, 4))
        if pairs:
            ax.scatter(range(1, len(pairs) + 1), [row["speedup_ch_over_gls"] for row in pairs], s=15)
            ax.set_yscale("log")
        else:
            ax.text(0.5, 0.5, "No paired successful CH/GLS runs", transform=ax.transAxes, ha="center")
        ax.axhline(1, color="gray", linestyle="--")
        ax.set(xlabel="Matched (task, repeat) pair", ylabel="T_CH / T_GLS",
               title=f"{group}: paired successful runs, n={len(pairs)}")
        fig.tight_layout()
        for suffix in ("png", "pdf"):
            fig.savefig(directory / f"speedup_{group}.{suffix}")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(7, 4))
        bottoms = [0.0] * len(methods)
        for field, label in (("generation_s", "Recorded generation"), ("llm_s", "LLM selection/repair"),
                             ("solver_s", "EBMC processes"), ("other_s", "Other Proof Time")):
            heights = []
            for i, method in enumerate(methods):
                values = [row.get(field) for row in selected if row["method"] == method and row["status"] != "MISSING"]
                summary = stats(values)
                heights.append(summary["mean"] or 0.0)
                if summary["missing"] or not summary["n"]:
                    ax.annotate(f"{field}: missing", (i, bottoms[i]), fontsize=6, rotation=90)
            ax.bar(methods, heights, bottom=bottoms, label=label)
            bottoms = [a + b for a, b in zip(bottoms, heights)]
        ax.set(ylabel="Mean seconds per observed run", title=f"{group}: cost decomposition (all observed outcomes)")
        ax.legend(fontsize=7)
        fig.tight_layout()
        for suffix in ("png", "pdf"):
            fig.savefig(directory / f"cost_{group}.{suffix}")
        plt.close(fig)


def cli(argv=None):
    project = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Summarize independent PS/CH/GLS repeats against the complete frozen task list")
    parser.add_argument("--pool", type=Path, default=project / "data/frozen_pool/experiment1_v1")
    parser.add_argument("--results", type=Path, default=project / "experiment1_results")
    parser.add_argument("--output", type=Path, default=project / "experiment1_results/summary")
    parser.add_argument("--methods", nargs="+", choices=METHODS, default=list(METHODS))
    parser.add_argument("--repeats", nargs="+", type=int, choices=(1, 2, 3), default=[1, 2, 3])
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args(argv)
    try:
        if len(set(args.methods)) != len(args.methods) or len(set(args.repeats)) != len(args.repeats):
            raise ValueError("Duplicate method or repeat")
        manifest, tasks, rows = collect(args.pool, args.results, args.methods, args.repeats,
                                       "smoke" if args.smoke else "formal")
        summary, paired = summarize(rows, args.methods, args.repeats)
        args.output.mkdir(parents=True, exist_ok=True)
        write_json(args.output / "summary.json", {
            "schema_version": 1, "pool_hash": manifest["pool_hash"],
            "run_scope": "smoke" if args.smoke else "formal", "groups": summary, "paired_ch_gls": paired,
            "notes": ["Standard deviation is the sample standard deviation.",
                      "Success counts are reported per repeat; no virtual-best union is used.",
                      "Missing runs remain in the denominator and have no invented time/query values.",
                      "Solver Time is EBMC process wall time, including frontend work.",
                      "Generation costs cover recorded responses; unavailable historical retry costs remain missing."]})
        write_json(args.output / "runs.json", rows)
        columns = ["method", "repeat", "group", "task_id", "status", *METRICS]
        with (args.output / "runs.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        with (args.output / "comparison.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["group", "method", "repeat", "total", *STATUSES, "success_rate"])
            for group, methods in summary.items():
                for method, data in methods.items():
                    for row in data["per_repeat"]:
                        writer.writerow([group, method, row["repeat"], row["total"],
                                         *[row["statuses"][status] for status in STATUSES], row["success_rate"]])
        with (args.output / "metrics.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["group", "method", "sample_scope", "metric", "n", "missing", "mean", "std", "median"])
            for group, methods in summary.items():
                for method, data in methods.items():
                    for sample_scope in ("metrics_proved", "metrics_all_observed"):
                        for metric, values in data[sample_scope].items():
                            writer.writerow([group, method, sample_scope, metric,
                                             *[values[field] for field in ("n", "missing", "mean", "std", "median")]])
        with (args.output / "paired_ch_gls.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["repeat", "task_id", "group", "speedup_ch_over_gls",
                                                       "query_difference_ch_minus_gls"])
            writer.writeheader()
            writer.writerows(paired)
        if not args.no_plots:
            figures(args.output, rows, args.methods, args.repeats, paired)
        print(f"Wrote {len(rows)} expected task/method/repeat records to {args.output}")
        return 0
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"experiment one summary: {exc}\n")
