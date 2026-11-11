"""Common deadline, EBMC execution and audit layer used by all three methods."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from evaluation import CacheMode, LemmaEvaluator, VerificationResult, VerilogModule
from gls import FormalQueryError, Query, Reply, Status
from gls_runtime import EbmcBackend
from pool import write_json
from formal_sva import PropertyResolutionError, property_rows, run_obligation, resolve_property_id
from backend_compat import candidate_error_id, lower_rank_reductions


class BudgetExhausted(TimeoutError):
    pass


class ProofFound(Exception):
    def __init__(self, entry):
        self.entry = entry


class RunContext:
    def __init__(self, directory: Path, budget_s: float, formal_timeout_s: float):
        self.directory = directory
        self.budget_s, self.formal_timeout_s = budget_s, formal_timeout_s
        self.started = None
        self.queries = []
        self.llm = []
        self.events = []
        self.phase, self.ids = "setup", ()

    def start(self):
        self.started = time.monotonic()

    def elapsed(self):
        return 0.0 if self.started is None else time.monotonic() - self.started

    def left(self):
        return max(0.0, self.budget_s - self.elapsed())

    def event(self, kind, **fields):
        self.events.append({"at_s": self.elapsed(), "kind": kind, **fields})

    @contextlib.contextmanager
    def operation(self, phase, ids=()):
        previous = self.phase, self.ids
        self.phase, self.ids = phase, tuple(ids)
        try:
            yield
        finally:
            self.phase, self.ids = previous

    def execute(self, command, timeout_s):
        """Only the process created by this call can be terminated on timeout."""
        if self.left() <= 0:
            raise BudgetExhausted("task wall budget exhausted before process launch")
        index = len(self.queries) + 1
        folder = self.directory / "formal_logs" / f"query_{index:04d}"
        folder.mkdir(parents=True)
        command = [str(x) for x in command]
        adaptations = []
        for index_arg, value in enumerate(command[1:], 1):
            if value.endswith((".sv", ".v")) and Path(value).is_file():
                source = folder / "input.sv"
                original = Path(value).read_text(encoding="utf-8")
                lowered, adaptations = lower_rank_reductions(original)
                if adaptations:
                    (folder / "input.original.sv").write_text(original, encoding="utf-8")
                source.write_text(lowered, encoding="utf-8")
                command[index_arg] = str(source.resolve())
                break
        cap = min(float(timeout_s), self.formal_timeout_s, self.left())
        if cap <= 0:
            raise BudgetExhausted("task wall budget exhausted during query preparation")
        row = {"index": index, "phase": self.phase, "ids": list(self.ids),
               "kind": "property_discovery" if "--show-properties" in command else "verification",
               "command": command, "timeout_s": cap, "at_s": self.elapsed(),
               "executed": False, "timed_out": False, "elapsed_s": 0.0,
               "returncode": None, "error": None}
        if adaptations:
            row["source_adaptations"] = adaptations
        self.queries.append(row)
        write_json(folder / "query.json", row)
        stdout = stderr = ""
        began = time.monotonic()
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, errors="replace", cwd=folder,
                                       start_new_session=True)
            row["executed"] = True
            write_json(folder / "query.json", row)
            try:
                stdout, stderr = process.communicate(timeout=cap)
            except subprocess.TimeoutExpired:
                row["timed_out"] = True
                # The group belongs only to this invocation, including any
                # translator child; never signal the caller's tmux process.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
            row["returncode"] = process.returncode
        except OSError as exc:
            row["error"] = str(exc)
        finally:
            row["elapsed_s"] = time.monotonic() - began
            (folder / "stdout.log").write_text(stdout, encoding="utf-8")
            (folder / "stderr.log").write_text(stderr, encoding="utf-8")
            write_json(folder / "query.json", row)
            with (self.directory / "formal_queries.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        if row["error"]:
            error = OSError(row["error"])
            raise error
        if row["timed_out"]:
            raise subprocess.TimeoutExpired(command, cap, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(command, row["returncode"], stdout, stderr)


def parse_ebmc(output: str, stderr: str, returncode: int, mode: str,
              expected_property: str | None = None) -> dict:
    """A bounded success is never accepted as an inductive proof."""
    result = {"verification_result": VerificationResult.INCONCLUSIVE,
              "log": output + stderr, "info": (output + stderr)[-2000:],
              "returncode": returncode}
    if (returncode not in (0, 10) or
            re.search(r"^\s*error:|invariant violation report", output + stderr,
                      re.MULTILINE | re.IGNORECASE)):
        result["verification_result"] = VerificationResult.ERROR
        return result
    rows = property_rows(output, expected_property)
    if expected_property is not None and not rows:
        result["info"] = "selected assertion result is missing: " + expected_property
        result["reason_code"] = "missing_obligation_result"
        return result
    induction = mode in {"gls_step", "ch_step", "one_induction", "one_inductive_with_prop"}
    if "REFUTED" in rows:
        result["verification_result"] = VerificationResult.CEX
        result["phase"] = "induction_base" if induction else mode
    elif rows and returncode == 0 and all(row == "PROVED" for row in rows):
        result["verification_result"] = VerificationResult.PROVEN
    elif (not induction and rows and returncode == 0 and
          all(row == "PROVED" or row.startswith("PROVED up to bound ") for row in rows)):
        result["verification_result"] = VerificationResult.PROVEN
    elif induction and "Induction Step" in output:
        base, step = output.split("Induction Step", 1)
        if (mode in {"gls_step", "ch_step"} and "UNSAT: No path" in base
                and "SAT: inductive proof failed" in step):
            result["verification_result"] = VerificationResult.CEX
            result["phase"] = "induction_step"
    elif returncode not in (0, 10):
        result["verification_result"] = VerificationResult.ERROR
    return result


class ExperimentModule(VerilogModule):
    def get_property(self):
        match = re.search(r"property\s+prop\s*;\s*(.*?)\s*endproperty", "".join(self.lines), re.S)
        if not match:
            raise ValueError("Target property prop is missing")
        return match.group(1)

    def get_directives(self, properties, mode):
        if mode in ("ch_initial", "ch_step"):
            names = [self.get_property_name(item) for item in properties]
            return [f"assert property({' and '.join(names)});\n"]
        return super().get_directives(properties, mode)


class ExperimentEvaluator(LemmaEvaluator):
    def __init__(self, rtl_path, ebmc_path, context: RunContext, pool):
        super().__init__(str(context.directory), "ebmc", str(rtl_path),
                         cache_mode=CacheMode.NO_CACHE, cache_result=False,
                         init_cache_with_no_cache_mode=False, ebmc_path=str(ebmc_path),
                         timeout=context.formal_timeout_s)
        self.module = ExperimentModule(str(rtl_path))
        self.context, self.pool = context, pool
        self.isolated_candidates = []

    def prepare_candidates(self):
        """Compile before selection; isolate only attributable candidate errors.

        This checks executability, never invariant truth. The original pool is
        retained for identity/audit, and every compiler process is budgeted.
        """
        if not self.pool:
            return {}
        remaining = dict(self.pool)
        with tempfile.TemporaryDirectory(prefix=".candidate_check_",
                                         dir=Path(self.module.filepath).parent) as folder:
            path = Path(folder) / "input.sv"
            # A clean goal-only compile distinguishes candidate errors from RTL
            # or environment failures before any candidate is excluded.
            def compile_set(candidates):
                self.module.clear_lemmas()
                texts = list(candidates.values())
                self.rename_lemmas(texts)
                renamed = {self.module.get_property_name(text): key
                           for key, text in zip(candidates, texts)}
                self.module.insert_lemmas_description(texts)
                self.module.insert_directive_for_properties(texts, "gls_initial", path)
                with self.context.operation("candidate_validation", tuple(candidates)):
                    completed = self.context.execute(
                        [self.ebmc_path, str(path), "--bound", "0", "--show-properties"],
                        self.context.formal_timeout_s)
                return completed, renamed

            try:
                clean, _ = compile_set({})
                resolve_property_id(clean, self.module.query_property_label)
                while remaining:
                    completed, renamed = compile_set(remaining)
                    try:
                        resolve_property_id(completed, self.module.query_property_label)
                        break
                    except PropertyResolutionError:
                        log = completed.stdout + completed.stderr
                        candidate = (candidate_error_id(log, path.read_text(), renamed)
                                     if completed.returncode > 0 else None)
                        if candidate is None:
                            raise
                        record = {"id": candidate, "reason_code": "candidate_compile_error",
                                  "returncode": completed.returncode, "backend_log": log}
                        self.isolated_candidates.append(record)
                        self.context.event("candidate_isolated", **record)
                        del remaining[candidate]
            except PropertyResolutionError as exc:
                answer = Query(Status.ERROR, detail=str(exc), log=exc.log,
                               returncode=exc.returncode, reason_code=exc.reason_code)
                raise FormalQueryError("candidate_validation", tuple(remaining), answer) from exc
            except subprocess.TimeoutExpired as exc:
                raise BudgetExhausted("candidate compilation exceeded its query timeout") from exc
            finally:
                self.module.clear_lemmas()
        return remaining

    def process_lemmas(self, lemmas, mode, **kwargs):
        if self.context.left() <= 0:
            raise BudgetExhausted("task wall budget exhausted")
        texts = [lemmas] if isinstance(lemmas, str) else lemmas
        ids = tuple(key for key, text in self.pool.items() if text in texts)
        before = len(self.context.queries)
        with self.context.operation(mode, ids):
            result = super().process_lemmas(lemmas, mode, **kwargs)
            if result["verification_result"] == VerificationResult.ERROR:
                rows = self.context.queries[before:]
                answer = Query(
                    Status.ERROR, result.get("time", 0), detail=result.get("info", ""),
                    log=result.get("log", ""), phase=mode,
                    executed=any(row["executed"] for row in rows),
                    returncode=result.get("returncode"),
                    reason_code=result.get("reason_code", "tool_error"),
                )
                self.context.event("formal_error", **FormalQueryError(mode, ids, answer).as_dict())
                raise FormalQueryError(mode, ids, answer)
            return result

    def _get_all_verification_results(self, lemmas):
        entry = super()._get_all_verification_results(lemmas)
        if self.solves(entry):
            # Stop the proof clock as soon as a complete singleton/prefix proof
            # exists, rather than spending the budget on later singletons.
            raise ProofFound(entry)
        return entry

    def execute_command(self, command, timeout_s):
        return self.context.execute(command, timeout_s)

    def run_ebmc(self, sv_file_path, mode, depth=30, trace=False):
        command = [self.ebmc_path, str(sv_file_path)]
        property_label = getattr(self.module, "query_property_label", None)
        if mode in ("gls_initial", "ch_initial"):
            command += ["--bound", "0"]
        elif mode in ("gls_bmc", "correctness_bounded"):
            command += ["--bound", "30"]
        elif mode == "gls_reachable_candidate":
            command += ["--bound", "1"]
        elif mode in ("gls_step", "ch_step", "one_induction", "one_inductive_with_prop"):
            command += ["--k-induction", "--bound", "1"]
        else:
            raise ValueError(f"Experiment one does not support mode: {mode}")
        if self.module.rst_exists:
            command += ["--reset", "rst"]
        command += ["--trace"]
        began = time.monotonic()
        try:
            # Native induction handles state properties without introducing
            # automata. Temporal conversion is an exact-diagnostic fallback,
            # shared by PS/CH/GLS within the original query deadline.
            induction = "--k-induction" in command
            attempts = (command, command + ["--buechi"]) if induction else (command,)
            for attempt_index, attempt in enumerate(attempts):
                cap = self.timeout - (time.monotonic() - began)
                if cap <= 0:
                    raise subprocess.TimeoutExpired(attempt, self.timeout)
                try:
                    completed, expected_property = run_obligation(
                        attempt, property_label, cap, execute=self.execute_command)
                    log, returncode = completed.stdout + completed.stderr, completed.returncode
                except PropertyResolutionError as exc:
                    log, returncode = exc.log, exc.returncode
                    if not (induction and attempt_index == 0 and
                            self._needs_temporal_conversion(returncode, log)):
                        raise
                else:
                    if not (induction and attempt_index == 0 and
                            self._needs_temporal_conversion(returncode, log)):
                        result = parse_ebmc(completed.stdout, completed.stderr, completed.returncode,
                                            mode, expected_property)
                        break
                self.context.event("induction_translation_fallback", mode=mode,
                                   reason_code="no_suitable_induction_property",
                                   native_returncode=returncode)
        except PropertyResolutionError as exc:
            result = {"verification_result": VerificationResult.ERROR, "info": str(exc),
                      "log": exc.log, "returncode": exc.returncode,
                      "reason_code": exc.reason_code}
        except subprocess.TimeoutExpired as exc:
            result = {"verification_result": VerificationResult.TIMEOUT,
                      "log": (exc.output or "") + (exc.stderr or ""), "info": "EBMC query timeout"}
        except OSError as exc:
            result = {"verification_result": VerificationResult.ERROR, "info": str(exc), "executed": False}
        result["time"] = time.monotonic() - began
        return result

    @staticmethod
    def _needs_temporal_conversion(returncode, log):
        return (returncode > 0 and re.search(
            r"^\s*error:\s*there is no property suitable for k-induction\s*$",
            log, re.MULTILINE | re.IGNORECASE) is not None)

    # Keep the reference PS classification/order/prefix loops; the common
    # induction routing above accounts for any required temporal conversion.
    def is_one_inductive(self, lemma):
        return self.process_lemmas(list(lemma) if isinstance(lemma, list) else [lemma], "one_induction")

    def is_one_inductive_with_property(self, lemma):
        return self.process_lemmas(list(lemma) if isinstance(lemma, list) else [lemma], "one_inductive_with_prop")


class ExperimentBackend(EbmcBackend):
    def _run(self, ids, mode, timeout_s):
        before = len(self.evaluator.context.queries)
        try:
            answer = super()._run(ids, mode, timeout_s)
        except BudgetExhausted as exc:
            return Query(Status.TIMEOUT, detail=str(exc), executed=False)
        answer.executed = any(row["executed"] for row in self.evaluator.context.queries[before:])
        return answer

    def candidates_initial(self, ids, timeout_s):
        return self._run(ids, "ch_initial", timeout_s)

    def candidates_step(self, ids, timeout_s):
        return self._run(ids, "ch_step", timeout_s)

    def _obligation(self, ids, timeout_s, *, candidates_only):
        from gls_step_witness import check_candidate_obligation
        candidate, *hypotheses = ids
        phase = "candidate_induction_obligation" if candidates_only else "joint_induction_obligation"
        with self.evaluator.context.operation(phase, hypotheses):
            self.evaluator.context.event("induction_obligation", candidate=candidate,
                                         hypotheses=hypotheses, candidates_only=candidates_only)
            try:
                answer = check_candidate_obligation(self.evaluator, self.pool,
                    tuple(hypotheses), candidate, timeout_s, candidates_only=candidates_only)
                if answer.status == Status.ERROR:
                    error = FormalQueryError(phase, tuple(hypotheses), answer)
                    self.evaluator.context.event("formal_error", **error.as_dict())
                    raise error
                return answer
            except BudgetExhausted as exc:
                return Query(Status.TIMEOUT, detail=str(exc), executed=False,
                             reason_code="task_budget_exhausted")

    def candidate_obligation(self, ids, timeout_s):
        return self._obligation(ids, timeout_s, candidates_only=False)

    def candidates_obligation(self, ids, timeout_s):
        return self._obligation(ids, timeout_s, candidates_only=True)

    def step_witness(self, ids, timeout_s, *, candidates_only=False):
        from gls_step_witness import find_step_witness
        with self.evaluator.context.operation("candidate_step_witness" if candidates_only else "step_witness", ids):
            try:
                answer = find_step_witness(self.evaluator, self.pool, ids, timeout_s,
                                           candidates_only=candidates_only)
                if answer.status == Status.ERROR:
                    phase = "candidate_step_witness" if candidates_only else "step_witness"
                    self.evaluator.context.event("formal_error", **FormalQueryError(phase, ids, answer).as_dict())
                    raise FormalQueryError(phase, ids, answer)
                return answer
            except BudgetExhausted as exc:
                return Query(Status.TIMEOUT, detail=str(exc), executed=False)


def precheck(backend, context):
    """Same goal-only initial / induction / depth-30 BMC policy as GLS."""
    initial = backend.initial((), min(context.formal_timeout_s, context.left()))
    if initial.status == Status.SAT:
        return "UNSAFE"
    if initial.status == Status.ERROR:
        return "ERROR"
    if initial.status == Status.UNSAT:
        step = backend.step((), min(context.formal_timeout_s, context.left()))
        if step.status == Status.UNSAT:
            return "PROVED"
        if step.status == Status.ERROR:
            return "ERROR"
    bmc = backend.bmc(30, min(context.formal_timeout_s, context.left()))
    if bmc.status == Status.SAT:
        return "UNSAFE"
    if bmc.status == Status.ERROR:
        return "ERROR"
    return None


class RecordedProposer:
    def __init__(self, profile, model_id, effort, context):
        self.profile, self.model_id, self.effort, self.context = profile, model_id, effort, context

    def propose(self, prompt, timeout_s):
        from openai import OpenAI
        cap = min(timeout_s, self.context.left())
        if cap <= 0:
            raise BudgetExhausted("no remaining LLM budget")
        directory = self.context.directory / "llm_logs" / f"request_{len(self.context.llm) + 1:03d}"
        request = {"model": self.model_id, "input": prompt,
                   "reasoning": {"effort": self.effort}}
        row = {"elapsed_s": 0.0, "input_tokens": None, "output_tokens": None,
               "status": "started", "timeout_s": cap}
        self.context.llm.append(row)
        write_json(directory / "request.json", request)
        write_json(directory / "metadata.json", row)
        began = time.monotonic()
        try:
            client = OpenAI(api_key=self.profile["key_file"].read_text().strip(),
                            base_url=self.profile["base_url"], timeout=cap, max_retries=0)
            response = client.responses.create(**request)
            raw = response.model_dump(mode="json")
            write_json(directory / "response.json", raw)
            usage = getattr(response, "usage", None)
            row["input_tokens"] = getattr(usage, "input_tokens", None)
            row["output_tokens"] = getattr(usage, "output_tokens", None)
            row["status"] = getattr(response, "status", "completed")
            if row["status"] != "completed":
                raise ValueError("Responses request did not complete")
            content = getattr(response, "output_text", "") or "".join(
                part.text for item in getattr(response, "output", ())
                for part in getattr(item, "content", ()) if isinstance(getattr(part, "text", None), str))
            if not content:
                raise ValueError("Responses request returned no text")
            return Reply(content, time.monotonic() - began,
                         row["input_tokens"] or 0, row["output_tokens"] or 0)
        except Exception as exc:
            row["status"] = "error"
            write_json(directory / "error.json", {"type": type(exc).__name__, "message": str(exc)})
            raise
        finally:
            row["elapsed_s"] = time.monotonic() - began
            write_json(directory / "metadata.json", row)
