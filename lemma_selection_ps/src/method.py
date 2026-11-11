"""PS: preserve the reference evaluator's classification, ordering and prefixes."""

from evaluation import LemmaEvaluator, VerificationResult
from gls import Result
from runtime import ProofFound, precheck

METHOD = "ps"


def run(backend, evaluator, context, pool):
    direct = precheck(backend, context)
    if direct:
        return Result(direct, direct_status=direct)
    if not pool:
        return Result("UNKNOWN", reason="empty candidate pool")
    try:
        entries = evaluator.evaluate_subsets(list(pool.values()))
    except ProofFound as proof:
        ids = tuple(key for key, text in pool.items() if text in proof.entry["lemma"])
        return Result("PROVED", selected_ids=ids)
    for entry in entries:
        if LemmaEvaluator.solves(entry):
            ids = tuple(key for key, text in pool.items() if text in entry["lemma"])
            return Result("PROVED", selected_ids=ids)
    timed_out = any(entry[field].get("verification_result") == VerificationResult.TIMEOUT
                    for entry in entries for field in
                    ("correct", "1-inductive", "1-inductive with property"))
    failed_launch = any(not row["executed"] and row["error"] for row in context.queries)
    return Result("ERROR" if failed_launch else "TIMEOUT" if timed_out else "UNKNOWN",
                  reason="reference prefix search exhausted")
