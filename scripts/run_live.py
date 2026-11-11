#!/usr/bin/env python3
"""Run one fresh experiment with the model and proxy in Codex config."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EBMC = ROOT / ".venv" / "bin" / "ebmc-5.9"


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return number


def load_codex_profile(config_path: Path) -> dict:
    """Read the selected Responses provider without executing auth commands."""
    with config_path.open("rb") as stream:
        config = tomllib.load(stream)
    provider_name = config["model_provider"]
    provider = config["model_providers"][provider_name]
    if provider["wire_api"] != "responses":
        raise ValueError("The selected Codex provider must use the Responses API")
    auth = provider.get("auth", {})
    if auth.get("command") != "cat" or len(auth.get("args", [])) != 1:
        raise ValueError(
            "The provider must use a single key file (auth.command=cat); "
            "arbitrary auth commands are not executed"
        )
    return {
        "provider": provider_name,
        "model": config["model"],
        "base_url": provider["base_url"],
        "reasoning_effort": config.get("model_reasoning_effort"),
        "key_file": Path(auth["args"][0]).expanduser(),
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-config", type=Path,
        default=Path.home() / ".codex" / "config.toml",
    )
    parser.add_argument("--model-id", help="Override the selected Codex model")
    parser.add_argument("--pipeline", choices=("agent", "non_agent"), required=True)
    parser.add_argument(
        "--modules-dir", type=Path,
        default=ROOT / "benchmarks" / "main_experiment",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True,
        help="New directory for this run's caches, prompts, logs, and results",
    )
    parser.add_argument("--few-shot", type=nonnegative_int, default=1)
    parser.add_argument("--fewshot-selection", choices=("cached", "live"),
                        default="cached")
    parser.add_argument("--num-samples", type=positive_int, default=5)
    parser.add_argument("--num-iterations", type=positive_int, default=7)
    parser.add_argument("--ebmc-timeout", type=positive_int, default=120)
    parser.add_argument("--time-budget-s", type=float)
    parser.add_argument(
        "--ebmc-path", default=str(DEFAULT_EBMC),
        help="EBMC 5.9 executable (default: project virtual environment)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        args.profile = load_codex_profile(args.codex_config.expanduser())
    except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError) as exc:
        parser.error(f"Cannot load selected Codex provider: {exc}")

    args.model_id = args.model_id or args.profile["model"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.model_id):
        parser.error("Model ID must be safe to use in an output directory name")
    parsed_url = urlsplit(args.profile["base_url"])
    if (parsed_url.scheme != "https" or not parsed_url.hostname
            or parsed_url.username or parsed_url.password or parsed_url.query):
        parser.error("Provider base URL must be HTTPS with no embedded credentials")
    key_file = args.profile["key_file"].resolve()
    if not key_file.is_file() or not key_file.read_text(encoding="utf-8").strip():
        parser.error(f"Proxy key file is missing or empty: {key_file}")
    if key_file.stat().st_mode & 0o077:
        parser.error(f"Proxy key file is not private (requires mode 0600): {key_file}")
    args.profile["key_file"] = key_file

    args.modules_dir = args.modules_dir.expanduser().resolve()
    if not args.modules_dir.is_dir():
        parser.error(f"Modules directory does not exist: {args.modules_dir}")
    suffixes = {".sv"} if args.pipeline == "agent" else {".sv", ".v"}
    args.module_files = sorted(
        path for path in args.modules_dir.iterdir()
        if path.is_file() and path.suffix in suffixes
    )
    if not args.module_files:
        parser.error(f"No supported modules in {args.modules_dir}")
    args.output_dir = args.output_dir.expanduser().absolute()
    if args.output_dir.exists() or args.output_dir.is_symlink():
        parser.error(f"Output directory already exists: {args.output_dir}")
    if args.time_budget_s is not None and args.time_budget_s <= 0:
        parser.error("--time-budget-s must be positive")
    executable = shutil.which(args.ebmc_path)
    if executable is None:
        parser.error(f"EBMC executable not found: {args.ebmc_path}")
    args.ebmc_path = Path(executable).resolve()
    version = subprocess.run(
        [str(args.ebmc_path), "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if not version.startswith("5.9"):
        parser.error(f"This experiment requires EBMC 5.9, found {version}")
    args.ebmc_version = version
    return args


def verify_output(args: argparse.Namespace, results_dir: Path) -> None:
    if args.pipeline == "agent":
        files = list(results_dir.glob("agent_conversations_*.json"))
        if len(files) != 1:
            raise RuntimeError("Missing agent conversation results")
        records = json.loads(files[0].read_text(encoding="utf-8"))
        missing = {p.name for p in args.module_files} - set(records)
        errors = [name for name, row in records.items()
                  if row.get("stop_reason") == "prompt_error"]
        if missing or errors:
            raise RuntimeError(f"Agent result incomplete: missing={sorted(missing)} errors={errors}")
    else:
        files = [p for p in results_dir.glob("proposed_lemmas_*.json")
                 if "_eval_" not in p.name]
        evaluations = list(results_dir.glob("*_eval_ebmc.json"))
        if len(files) != 1 or len(evaluations) != 1:
            raise RuntimeError("Missing generated lemmas or EBMC evaluation results")
        proposed = json.loads(files[0].read_text(encoding="utf-8"))
        evaluated = json.loads(evaluations[0].read_text(encoding="utf-8"))
        expected = {p.stem for p in args.module_files}
        missing = expected - set(proposed) | expected - set(evaluated)
        if missing:
            raise RuntimeError(f"Non-agent result missing modules: {sorted(missing)}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    job_name = (f"{args.model_id}_{args.pipeline}_{args.few_shot}_"
                f"{args.modules_dir.name}")
    results_dir = args.output_dir / args.modules_dir.name / job_name / "results"
    command = [
        sys.executable, str(ROOT / "scripts" / "entry_point.py"),
        "--live-model-id", args.model_id,
        "-pipeline", args.pipeline,
        "-modules_dir", str(args.modules_dir),
        "-storage_dir", str(args.output_dir),
        "-evaluation_cache_mode", "NO_CACHE",
        "-few_shot", str(args.few_shot),
        "-num_samples", str(args.num_samples),
        "-num_iterations", str(args.num_iterations),
        "-ebmc_timeout", str(args.ebmc_timeout),
        "--fewshot-selection", args.fewshot_selection,
    ]
    if args.time_budget_s is not None:
        command.extend(("-time_budget_s", str(args.time_budget_s)))
    env = os.environ.copy()
    env.update({
        "LLM_API_KEY_FILE": str(args.profile["key_file"]),
        "LLM_BASE_URL": args.profile["base_url"],
        "LLM_WIRE_API": "responses",
        "EBMC_PATH": str(args.ebmc_path),
    })
    if args.pipeline == "non_agent":
        env["LLM_GLS_ENABLED"] = "1"
        env["LLM_GLS_BUDGET_S"] = str(args.time_budget_s or 600)
    if args.profile["reasoning_effort"]:
        env["LLM_REASONING_EFFORT"] = args.profile["reasoning_effort"]
    else:
        env.pop("LLM_REASONING_EFFORT", None)

    manifest = {
        "schema_version": 1,
        "kind": "live",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "provider": args.profile["provider"],
        "base_url": args.profile["base_url"],
        "wire_api": "responses",
        "model_id": args.model_id,
        "reasoning_effort": args.profile["reasoning_effort"],
        "pipeline": args.pipeline,
        "few_shot": args.few_shot,
        "fewshot_selection": args.fewshot_selection,
        "num_samples": args.num_samples,
        "num_iterations": args.num_iterations,
        "ebmc_timeout_seconds": args.ebmc_timeout,
        "ebmc_version": args.ebmc_version,
        "ebmc_sha256": sha256(args.ebmc_path),
        "module_hashes": {p.name: sha256(p) for p in args.module_files},
    }
    if args.dry_run:
        manifest["status"] = "dry_run"
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return 0

    args.output_dir.mkdir(parents=True)
    manifest_path = args.output_dir / "live-run.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Running {args.model_id} / {args.pipeline} / fs{args.few_shot} "
          f"on {len(args.module_files)} modules", flush=True)
    print(f"Output: {args.output_dir}", flush=True)
    completed = subprocess.run(command, env=env, check=False)
    try:
        if completed.returncode != 0:
            raise RuntimeError(f"Experiment process exited with {completed.returncode}")
        verify_output(args, results_dir)
    except RuntimeError as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        result = 1
    else:
        manifest["status"] = "completed"
        result = 0
    manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if result:
        print(manifest["error"], file=sys.stderr)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
