"""Versioned fixed-pool packages. No generation or formal queries occur here."""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

GROUP_COUNTS = {"main_experiment": 78, "hard": 31}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def pool_mapping(texts: list[str]) -> dict[str, str]:
    unique = list(dict.fromkeys(texts))
    width = max(2, len(str(len(unique))))
    return {f"L{i:0{width}d}": text for i, text in enumerate(unique, 1)}


def candidate_hash(pool: dict[str, str]) -> str:
    # Match the existing GLS selector, including its JSON whitespace convention.
    return digest(json.dumps(list(pool.items()), ensure_ascii=False).encode("utf-8"))


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or Path(relative).is_absolute():
        raise ValueError(f"Path escapes frozen package: {relative}")
    return path


def load_pool(directory: Path) -> tuple[dict, list[dict]]:
    directory = directory.resolve()
    if not (directory / "manifest.json").is_file():
        raise ValueError(f"Frozen pool is not ready: {directory}/manifest.json is missing")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("status") != "frozen":
        raise ValueError("Unsupported or unfinished frozen pool")
    body = {k: v for k, v in manifest.items() if k != "pool_hash"}
    if digest(canonical(body)) != manifest.get("pool_hash"):
        raise ValueError("Frozen manifest hash mismatch")
    for name, expected in manifest["files"].items():
        if digest(inside(directory, name).read_bytes()) != expected:
            raise ValueError(f"Frozen file hash mismatch: {name}")
    if "tasks.json" not in manifest["files"]:
        raise ValueError("tasks.json is not covered by the manifest")
    tasks = json.loads((directory / "tasks.json").read_text(encoding="utf-8"))
    seen = set()
    counts = {}
    for task in tasks:
        group, name = task["group"], task["name"]
        if group not in GROUP_COUNTS or name in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
            raise ValueError("Invalid task group/name")
        if task["task_id"] != f"{group}/{name}" or task["task_id"] in seen:
            raise ValueError("Duplicate or inconsistent task ID")
        seen.add(task["task_id"])
        counts[group] = counts.get(group, 0) + 1
        if task["rtl_path"] not in manifest["files"]:
            raise ValueError("RTL is not covered by the manifest")
        rtl = inside(directory, task["rtl_path"]).read_bytes()
        if digest(rtl) != task["rtl_sha256"]:
            raise ValueError("Task RTL hash mismatch")
        if digest(task["property_text"].encode()) != task["property_sha256"]:
            raise ValueError("Property hash mismatch")
        target = re.search(r"property\s+prop\s*;\s*(.*?)\s*endproperty", rtl.decode("utf-8"), re.S)
        if not target or target.group(1) != task["property_text"]:
            raise ValueError("Frozen property differs from the RTL")
        if task["generation_path"] not in manifest["files"]:
            raise ValueError("Generation audit is not covered by the manifest")
        candidates = task["candidates"]
        mapping = {row["id"]: row["text"] for row in candidates}
        if list(mapping.items()) != list(pool_mapping([r["text"] for r in candidates]).items()):
            raise ValueError("Candidate IDs, order or exact-text deduplication mismatch")
        if len(mapping) != len(candidates):
            raise ValueError("Duplicate candidate ID")
        for row in candidates:
            if not isinstance(row["text"], str) or digest(row["text"].encode()) != row["sha256"]:
                raise ValueError("Candidate content hash mismatch")
        if candidate_hash(mapping) != task["candidate_pool_hash"]:
            raise ValueError("Candidate pool hash mismatch")
    if not tasks or counts != manifest["task_counts"]:
        raise ValueError("Missing tasks or inconsistent task counts")
    if manifest["scope"] == "formal" and counts != GROUP_COUNTS:
        raise ValueError("Formal pool must contain 78 main and 31 hard tasks")
    if manifest["scope"] not in ("formal", "smoke"):
        raise ValueError("Invalid pool scope")
    return manifest, tasks


def empty_destination(path: Path) -> bool:
    return not path.exists() or (path.is_dir() and
                                all(p.name == ".gitkeep" for p in path.iterdir()))


def export_pool(records: list[dict], destination: Path, *, provenance: dict,
                scope: str = "formal") -> dict:
    """Records contain source RTL and exact candidate text, with response audit data."""
    if not empty_destination(destination):
        raise ValueError(f"Refusing to replace an existing pool: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".freeze_", dir=destination.parent) as folder:
        staging = Path(folder) / "package"
        staging.mkdir()
        tasks = []
        for record in records:
            group, name = record["group"], record["name"]
            if group not in GROUP_COUNTS or name in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
                raise ValueError("Invalid task group/name")
            rtl = record["rtl"]
            # Only executable tokens reference files; comments and strings can
            # mention disabled includes or readmem calls without dependencies.
            code = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', " ", rtl, flags=re.S)
            if re.search(r'`include\b|\$readmem[hb]\b', code):
                raise ValueError(f"{group}/{name}: external RTL dependencies need explicit packaging")
            match = re.search(r"property\s+prop\s*;\s*(.*?)\s*endproperty", rtl, re.S)
            if not match:
                raise ValueError(f"{group}/{name}: target property prop is missing")
            target = match.group(1)
            relative = f"rtl/{group}/{name}.sv"
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rtl, encoding="utf-8")
            mapping = pool_mapping(record["lemmas"])
            generation_path = f"generation/{group}/{name}.json"
            write_json(staging / generation_path, record.get("generation", {}))
            tasks.append({
                "task_id": f"{group}/{name}", "group": group, "name": name,
                "rtl_path": relative, "rtl_sha256": digest(path.read_bytes()),
                "property_text": target, "property_sha256": digest(target.encode()),
                "environment": {"assumptions": "preserved verbatim in RTL",
                                "reset_signal": "rst" if re.search(
                                    r"\binput(?:\s+(?:reg|logic|wire))?\s+rst\b", rtl) else None},
                "candidates": [{"id": key, "text": text, "sha256": digest(text.encode())}
                               for key, text in mapping.items()],
                "candidate_pool_hash": candidate_hash(mapping),
                "generation_path": generation_path,
                "generation_cost": record.get("generation_cost", {
                    "time_s": None, "calls": None, "input_tokens": None,
                    "output_tokens": None, "missing": ["generation audit unavailable"]}),
            })
        write_json(staging / "tasks.json", tasks)
        counts = {group: sum(t["group"] == group for t in tasks)
                  for group in GROUP_COUNTS if any(t["group"] == group for t in tasks)}
        manifest = {
            "schema_version": 1, "status": "frozen", "scope": scope,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "task_counts": counts, "deduplication": "exact text, first-seen order",
            "provenance": provenance,
            "files": {str(p.relative_to(staging)): digest(p.read_bytes())
                      for p in sorted(staging.rglob("*")) if p.is_file()},
        }
        manifest["pool_hash"] = digest(canonical(manifest))
        write_json(staging / "manifest.json", manifest)
        load_pool(staging)
        if destination.exists():
            (destination / ".gitkeep").unlink(missing_ok=True)
            destination.rmdir()
        staging.rename(destination)
    return manifest


def copy_pool(source: Path, destination: Path) -> None:
    manifest, _ = load_pool(source)
    if not empty_destination(destination):
        existing, _ = load_pool(destination)
        if existing["pool_hash"] == manifest["pool_hash"]:
            return
        raise ValueError(f"Refusing to replace a different frozen pool: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pool_copy_", dir=destination.parent) as folder:
        staging = Path(folder) / "package"
        shutil.copytree(source, staging)
        load_pool(staging)
        if destination.exists():
            (destination / ".gitkeep").unlink(missing_ok=True)
            destination.rmdir()
        staging.rename(destination)


def parse_response(response: dict) -> tuple[list[str], str]:
    match = re.search(r"<json>\s*(\{.*?\})\s*</json>", response.get("text", ""), re.S | re.I)
    if not match:
        return [], "invalid_json_block"
    try:
        rows = json.loads(match.group(1))["lemmas"]
        if not isinstance(rows, list):
            raise ValueError("lemmas is not a list")
        lemmas = [row["lemma"] if "lemma" in row else next(iter(row.values())) for row in rows]
        if any(not isinstance(text, str) for text in lemmas):
            raise ValueError("non-string candidate")
        return lemmas, "parsed" if lemmas else "empty"
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
        return [], "parse_error"


def source_records(root: Path, project: Path, *, smoke: bool = False) -> tuple[list[dict], dict]:
    """Read completed live outputs and their five cached generation responses only."""
    from diskcache import Disk
    records, provenance = [], {"source_root": str(root.resolve()), "groups": {}}
    final_index = None
    index_path = root / "manifest.json"
    if index_path.is_file():
        final_index = json.loads(index_path.read_text(encoding="utf-8"))
        if (final_index.get("schema_version") != 1 or
                final_index.get("kind") != "gls_prerun_final" or
                final_index.get("status") != "completed"):
            raise ValueError("Unsupported or unfinished final-results index")
        index_body = {k: v for k, v in final_index.items() if k != "manifest_hash"}
        if digest(canonical(index_body)) != final_index.get("manifest_hash"):
            raise ValueError("Final-results manifest hash mismatch")
        for name, expected in final_index["files"].items():
            if digest(inside(root, name).read_bytes()) != expected:
                raise ValueError(f"Final-results file hash mismatch: {name}")
        if set(final_index["groups"]) != set(GROUP_COUNTS):
            raise ValueError("Final-results index must contain main and hard")
    for group, expected_count in GROUP_COUNTS.items():
        group_root = root / group
        if final_index is not None:
            entry = final_index["groups"][group]
            manifest = entry["config"]
            indexed_files = [entry["candidates"], *[
                str(p.relative_to(root)) for p in inside(root, entry["prompts"]).glob("*.json")],
                *[str(p.relative_to(root)) for p in inside(root, entry["llm_cache"]).rglob("*") if p.is_file()]]
            if any(name not in final_index["files"] for name in indexed_files):
                raise ValueError(f"{group}: final input is not covered by file hashes")
            path = inside(root, entry["candidates"])
            prompts_dir = inside(root, entry["prompts"])
            cache_dir = inside(root, entry["llm_cache"])
            if not (cache_dir / "cache.db").is_file():
                raise ValueError(f"{group}: indexed generation cache is missing")
        else:
            manifest_path = group_root / "live-run.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            generated = [p for p in group_root.glob("**/results/proposed_lemmas_*.json")
                         if "_eval_" not in p.name]
            if len(generated) != 1:
                raise ValueError(f"{group}: expected one generated candidate JSON")
            path = generated[0]
            job = path.parent.parent
            prompts_dir = job / "work/prompts"
            cache_dir = job / "llm_cache"
        if manifest.get("status") != "completed":
            raise ValueError(f"{group}: generation/evaluation is still unfinished; no live cache is copied")
        if manifest.get("num_samples") != 5 or manifest.get("pipeline") != "non_agent":
            raise ValueError(f"{group}: expected the five-sample non-agent generation")
        if not smoke and (manifest.get("model_id") != "gpt-6.1-sol" or
                          manifest.get("reasoning_effort") != "xhigh" or
                          manifest.get("few_shot") != 1 or
                          manifest.get("fewshot_selection") != "cached"):
            raise ValueError(f"{group}: generation configuration differs from the planned fixed configuration")
        proposed = json.loads(path.read_text(encoding="utf-8"))
        prompts = {}
        for prompt_path in sorted(prompts_dir.glob("*.json")):
            prompt = json.loads(prompt_path.read_text(encoding="utf-8"))
            if prompt["module_name"] in prompts:
                raise ValueError("Duplicate generation prompt")
            prompts[prompt["module_name"]] = prompt
        modules = {p.stem: p for p in (project / "benchmarks" / group).iterdir()
                   if p.suffix in (".v", ".sv") and p.is_file()}
        if set(proposed) != set(modules) or set(prompts) != set(modules):
            raise ValueError(f"{group}: task/prompt/generated coverage mismatch")
        if not smoke and len(modules) != expected_count:
            raise ValueError(f"{group}: expected {expected_count} tasks")
        # Original live caches may still use WAL. Final packages contain a
        # self-contained snapshot; immutable reads create no journal sidecars.
        options = "?mode=ro&immutable=1" if final_index is not None else "?mode=ro"
        connection = sqlite3.connect((cache_dir / "cache.db").resolve().as_uri() + options, uri=True)
        try:
            settings = dict(connection.execute("SELECT key,value FROM Settings"))
            disk = Disk(str(cache_dir), min_file_size=int(settings.get("disk_min_file_size", 0)),
                        pickle_protocol=int(settings.get("disk_pickle_protocol", 0)))
            for name, module_path in sorted(modules.items()):
                if manifest.get("module_hashes", {}).get(module_path.name) != digest(module_path.read_bytes()):
                    raise ValueError(f"{group}/{name}: RTL differs from the generation snapshot")
                prompt = prompts[name]
                model, representation = prompt["model_name"], prompt["representation"]
                messages = prompt.get("prompt", prompt.get("messages"))
                if isinstance(messages, list):
                    prompt_text = messages[0]["content"] if len(messages) == 1 else json.dumps(messages, separators=(",", ":"))
                else:
                    prompt_text = messages
                key = digest(f"{prompt_text}-{model}".encode())
                database_key, raw = disk.put(key)
                row = connection.execute("SELECT mode,filename,value FROM Cache WHERE key=? AND raw=?",
                                         (database_key, raw)).fetchone()
                if row is None:
                    raise ValueError(f"{group}/{name}: generation cache miss")
                responses = disk.fetch(*row, read=False)
                if len(responses) != 5:
                    raise ValueError(f"{group}/{name}: expected exactly five generation responses")
                # The original generator samples the cache with random.seed(42).
                selected = random.Random(42).sample(list(enumerate(responses)), k=5)
                lemmas, audit = [], []
                for source_index, response in selected:
                    extracted, status = parse_response(response)
                    lemmas.extend(extracted)
                    audit.append({"source_index": source_index, "response": response,
                                  "parse_status": status, "candidate_count": len(extracted)})
                if lemmas != proposed[name][model][representation]["lemmas"]:
                    raise ValueError(f"{group}/{name}: re-extraction differs from the saved candidate order")
                cost = {"calls": 5, "missing": ["historical failed attempts/retry costs unavailable"]}
                for field, output in (("time", "time_s"), ("input_tokens", "input_tokens"),
                                      ("output_tokens", "output_tokens")):
                    values = [r.get(field) for r in responses]
                    if any(not isinstance(v, (float, int)) or v < 0 for v in values):
                        cost[output] = None
                        cost["missing"].append(field)
                    else:
                        cost[output] = sum(values)
                records.append({"group": group, "name": name, "rtl": module_path.read_text(encoding="utf-8"),
                                "lemmas": lemmas, "generation_cost": cost,
                                "generation": {"prompt": prompt, "responses": audit, "cache_key": key}})
        finally:
            connection.close()
        provenance["groups"][group] = {"run_manifest": manifest,
                                      "generated_json_sha256": digest(path.read_bytes())}
    return records, provenance
