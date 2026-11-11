"""Adapters from the existing non-agent JSON/EBMC pipeline to LLM-GLS."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from evaluation import LemmaEvaluator, VerificationResult
from gls import PROMPT_VERSION, Config, Query, Reply, Selector, Status


class EbmcBackend:
    def __init__(self, evaluator: LemmaEvaluator, pool: dict[str, str]):
        if evaluator.tool != "ebmc":
            raise ValueError("LLM-GLS requires the EBMC backend")
        self.evaluator = evaluator
        self.pool = pool

    def _run(self, ids: tuple[str, ...], mode: str, timeout_s: float) -> Query:
        original_timeout = self.evaluator.timeout
        try:
            self.evaluator.timeout = timeout_s
            raw = self.evaluator.process_lemmas(
                [self.pool[x] for x in ids], mode=mode, trace=True
            )
        finally:
            self.evaluator.timeout = original_timeout
        state = raw["verification_result"]
        status = {
            VerificationResult.PROVEN: Status.UNSAT,
            VerificationResult.CEX: Status.SAT,
            VerificationResult.TIMEOUT: Status.TIMEOUT,
            VerificationResult.INCONCLUSIVE: Status.UNKNOWN,
            VerificationResult.UNCACHED: Status.UNKNOWN,
            VerificationResult.ERROR: Status.ERROR,
        }[state]
        if mode == "gls_step" and raw.get("phase") == "induction_base":
            # A reachable base refutation is not an arbitrary-state step witness.
            status = Status.UNKNOWN
        detail = str(raw.get("info", ""))[:2000]
        if raw.get("phase") == "induction_base":
            detail = "induction base refuted; " + detail
        return Query(status, elapsed_s=raw.get("time", 0), detail=detail,
                     log=raw.get("log", ""), phase=raw.get("phase"),
                     executed=raw.get("executed", not raw.get("malformed", False)),
                     p_initial=False if mode == "gls_initial" and not ids
                     and status == Status.SAT else None,
                     reason_code=raw.get("reason_code", "backend_inconclusive" if status == Status.UNKNOWN else ""),
                     returncode=raw.get("returncode"))

    def initial(self, ids: tuple[str, ...], timeout_s: float) -> Query:
        return self._run(ids, "gls_initial", timeout_s)

    def step(self, ids: tuple[str, ...], timeout_s: float) -> Query:
        return self._run(ids, "gls_step", timeout_s)

    def step_witness(self, ids: tuple[str, ...], timeout_s: float) -> Query:
        from gls_step_witness import find_step_witness
        return find_step_witness(self.evaluator, self.pool, ids, timeout_s)

    def candidate_obligation(self, ids: tuple[str, ...], timeout_s: float) -> Query:
        from gls_step_witness import check_candidate_obligation
        candidate, *hypotheses = ids
        return check_candidate_obligation(self.evaluator, self.pool, tuple(hypotheses),
                                          candidate, timeout_s)

    def reachable(self, ids: tuple[str, ...], timeout_s: float) -> Query:
        if len(ids) != 1:
            raise ValueError("reachable-candidate check requires one ID")
        return self._run(ids, "gls_reachable_candidate", timeout_s)

    def bmc(self, depth: int, timeout_s: float) -> Query:
        if depth != 30:
            raise ValueError("This EBMC adapter is configured for the shared depth-30 BMC")
        return self._run((), "gls_bmc", timeout_s)


class RelayProposer:
    """Use the same Codex-configured Responses relay as scripts/run_live.py."""

    def __init__(self, model_id: str):
        if os.getenv("LLM_WIRE_API") != "responses":
            raise ValueError("LLM-GLS requires the configured Responses relay")
        self.base_url = os.getenv("LLM_BASE_URL")
        key_file = os.getenv("LLM_API_KEY_FILE")
        if not self.base_url or not key_file:
            raise ValueError("LLM_BASE_URL and LLM_API_KEY_FILE are required")
        self.api_key = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
        if not self.api_key:
            raise ValueError("The Responses relay key file is empty")
        self.model_id = model_id
        self.effort = os.getenv("LLM_REASONING_EFFORT")

    def propose(self, prompt: str, timeout_s: float) -> Reply:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                        timeout=timeout_s, max_retries=0)
        options = {"reasoning": {"effort": self.effort}} if self.effort else {}
        began = time.monotonic()
        response = client.responses.create(model=self.model_id, input=prompt, **options)
        elapsed = time.monotonic() - began
        if getattr(response, "status", "completed") != "completed":
            raise ValueError("Responses request did not complete")
        content = getattr(response, "output_text", "") or "".join(
            part.text for item in getattr(response, "output", ())
            for part in getattr(item, "content", ())
            if isinstance(getattr(part, "text", None), str)
        )
        if not content:
            raise ValueError("Responses request returned no text")
        usage = getattr(response, "usage", None)
        return Reply(content, elapsed,
                     getattr(usage, "input_tokens", 0) or 0,
                     getattr(usage, "output_tokens", 0) or 0)


def legacy_entries(selector: Selector) -> list[dict]:
    """Keep the original evaluation JSON field names and numeric result codes."""
    unknown = VerificationResult.UNCACHED

    def entry(ids: tuple[str, ...], proof: VerificationResult) -> dict:
        return {
            "lemma": [selector.pool[x] for x in ids],
            "correct": {"verification_result": unknown},
            "1-inductive": {"verification_result": unknown},
            "implies": {"verification_result": unknown},
            "implied": {"verification_result": unknown},
            "1-inductive with property": {"verification_result": proof},
        }

    by_id = {candidate: entry((candidate,), unknown) for candidate in selector.pool}
    groups = []
    mapped = {
        "PROVED": VerificationResult.PROVEN,
        "INITIAL_SAT": VerificationResult.CEX,
        "STEP_SAT": VerificationResult.CEX,
        "TIMEOUT": VerificationResult.TIMEOUT,
        "ERROR": VerificationResult.ERROR,
        "UNKNOWN": VerificationResult.INCONCLUSIVE,
    }
    for checked in selector.result.checked:
        ids = tuple(checked["ids"])
        item = entry(ids, mapped.get(checked["status"], unknown))
        if len(ids) == 1:
            by_id[ids[0]] = item
        else:
            groups.append(item)
    if selector.result.status == "PROVED" and selector.result.selected_ids:
        ids = selector.result.selected_ids
        if not any(row["lemma"] == [selector.pool[x] for x in ids]
                   for row in groups):
            item = entry(ids, VerificationResult.PROVEN)
            if len(ids) == 1:
                by_id[ids[0]] = item
            else:
                groups.append(item)
    if selector.result.status == "PROVED" and not selector.result.selected_ids:
        groups.append(entry((), VerificationResult.PROVEN))
    return [*by_id.values(), *groups]


def run_selection(evaluator: LemmaEvaluator, model_id: str,
                  lemmas: list[str], output_path: Path) -> tuple[list[dict], int]:
    # Preserve first-seen order and use only exactly identical SVA text for dedup.
    candidates = list(dict.fromkeys(lemmas))
    priors = {}
    if os.getenv("LLM_GLS_PRIORS_FILE"):
        priors = json.loads(Path(os.environ["LLM_GLS_PRIORS_FILE"]).read_text(
            encoding="utf-8"
        ))
    config = Config(
        budget_s=float(os.getenv("LLM_GLS_BUDGET_S", "600")),
        formal_timeout_s=evaluator.timeout,
        initial_cost_s={int(k): float(v) for k, v in priors.get("initial_s", {}).items()},
        step_cost_s={int(k): float(v) for k, v in priors.get("step_s", {}).items()},
        llm_estimate_s=priors.get("llm_s"),
        houdini_estimate_s=priors.get("houdini_s"),
    )
    selector = Selector("".join(evaluator.module.lines), evaluator.module.get_property(),
                        candidates, backend=None, proposer=None, config=config)
    selector.backend = EbmcBackend(evaluator, selector.pool)
    selector.proposer = RelayProposer(model_id)
    result = selector.run()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "schema_version": 2,
        "method": "LLM-GLS",
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "random_seed": None,
        "pool_hash": selector.pool_hash,
        "candidate_ids": selector.pool,
        "config": config.__dict__,
        "status": result.status,
        "selected_ids": list(result.selected_ids),
        "direct_status": result.direct_status,
        "reason": result.reason,
        "wall_s": result.wall_s,
        "formal_queries": result.formal_queries,
        "selection_queries": result.selection_queries,
        "llm_calls": result.llm_calls,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "error": result.error,
        "checked": result.checked,
        "events": result.events,
    }
    output_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    property_status = {
        "PROVED": VerificationResult.PROVEN,
        "UNSAFE": VerificationResult.CEX,
        "ERROR": VerificationResult.ERROR,
        "TIMEOUT": VerificationResult.TIMEOUT,
    }.get(result.direct_status, VerificationResult.INCONCLUSIVE)
    if result.status == "UNSAFE":
        property_status = VerificationResult.CEX
    return legacy_entries(selector), property_status.value
