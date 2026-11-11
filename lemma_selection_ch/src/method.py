"""Classical Houdini: learn candidate invariants without assuming the goal."""

from gls import Result, Status
from runtime import precheck

METHOD = "ch"


def run(backend, evaluator, context, pool):
    direct = precheck(backend, context)
    if direct:
        return Result(direct, direct_status=direct)
    remaining = tuple(pool)

    def query(name, ids):
        return getattr(backend, name)(ids, min(context.formal_timeout_s, context.left()))

    def remove_reachable(ids):
        removed = []
        for candidate in ids:
            answer = query("reachable", (candidate,))
            if answer.status == Status.SAT:
                removed.append(candidate)
            elif answer.status != Status.UNSAT:
                return removed, answer.status.value
        return removed, None

    while remaining:
        witness = None
        initial = query("candidates_initial", remaining)
        if initial.status == Status.SAT:
            removed, failure = remove_reachable(remaining)
            if failure:
                return Result(failure)
            if not removed:
                return Result("UNKNOWN", reason="candidate initial failure could not be localized",
                              reason_code="candidate_initial_failure_not_localized")
        elif initial.status != Status.UNSAT:
            return Result(initial.status.value, reason=initial.detail,
                          reason_code=initial.reason_code or "candidate_initial_check_inconclusive")
        else:
            step = query("candidates_step", remaining)
            if step.status == Status.UNSAT:
                break  # The candidate set is an inductive fixed point.
            if step.phase == "induction_base":
                removed, failure = remove_reachable(remaining)
                if failure:
                    return Result(failure)
            elif step.status == Status.SAT:
                witness = backend.step_witness(remaining,
                    min(context.formal_timeout_s, context.left()), candidates_only=True)
                if witness.status in (Status.ERROR, Status.TIMEOUT):
                    return Result(witness.status.value, reason=witness.detail,
                                  reason_code=witness.reason_code or "query_timeout")
                removed = [key for key in remaining
                           if witness.status == Status.SAT and
                           witness.next_state is not None and witness.next_state.get(key) is False]
                if not removed and hasattr(backend, "candidates_obligation"):
                    # Check each obligation under ALL current hypotheses. These
                    # are not independent singleton invariant checks.
                    for candidate in remaining:
                        answer = query("candidates_obligation", (candidate, *remaining))
                        if answer.status == Status.SAT:
                            removed.append(candidate)
                        elif answer.status in (Status.ERROR, Status.TIMEOUT):
                            return Result(answer.status.value, reason=answer.detail,
                                          reason_code=answer.reason_code or "query_timeout")
                        else:
                            witness = answer
            else:
                return Result(step.status.value, reason=step.detail,
                              reason_code=step.reason_code or "candidate_step_check_inconclusive")
            if not removed:
                return Result("UNKNOWN", reason="no sound candidate-deletion witness" +
                              (": " + witness.detail if witness else ""),
                              reason_code=(witness.reason_code if witness else "") or
                              "candidate_failure_not_localized")
        context.event("houdini_remove", ids=removed, candidates_only=True)
        remaining = tuple(key for key in remaining if key not in removed)
    # The goal is checked only against the resulting inductive candidate set.
    initial = query("initial", remaining)
    if initial.status != Status.UNSAT:
        return Result("UNKNOWN" if initial.status == Status.SAT else initial.status.value,
                      reason="target initiality was not established",
                      reason_code="target_initiality_not_established")
    step = query("step", remaining)
    if step.status == Status.UNSAT:
        return Result("PROVED", selected_ids=remaining)
    return Result("UNKNOWN" if step.status == Status.SAT else step.status.value,
                  reason="inductive fixed point does not prove the target",
                  reason_code="candidate_fixed_point_insufficient" if step.status == Status.SAT
                  else "query_timeout" if step.status == Status.TIMEOUT else "target_check_inconclusive")
