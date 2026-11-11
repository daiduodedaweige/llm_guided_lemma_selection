"""Fixed-pool LLM-GLS selection. Formal answers, never model text, decide proof."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


PROMPT_VERSION = "gls-fixed-pool-v3-houdini-prescreen"


class Status(str, Enum):
    SAT = "SAT"
    UNSAT = "UNSAT"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


@dataclass
class Query:
    status: Status
    elapsed_s: float = 0.0
    detail: str = ""
    log: str = ""
    phase: str | None = None
    executed: bool = True
    # Values must come from the formal backend under the same SVA semantics.
    source: dict[str, bool | None] | None = None
    next_state: dict[str, bool | None] | None = None
    p_initial: bool | None = None
    p_next: bool | None = None
    returncode: int | None = None
    reason_code: str = ""


class FormalQueryError(Exception):
    """Stop one task after its first formal-tool error, preserving the evidence."""

    def __init__(self, phase: str, ids: tuple[str, ...], answer: Query):
        self.phase, self.ids, self.answer = phase, ids, answer
        super().__init__(f"Formal tool error in {phase}, ids={list(ids)}: {answer.detail}")

    def as_dict(self) -> dict:
        return {"type": type(self).__name__, "phase": self.phase,
                "ids": list(self.ids), "message": self.answer.detail,
                "returncode": self.answer.returncode,
                "reason_code": self.answer.reason_code,
                "executed": self.answer.executed, "backend_log": self.answer.log}


@dataclass
class Reply:
    text: str
    elapsed_s: float
    input_tokens: int = 0
    output_tokens: int = 0


class Backend(Protocol):
    def initial(self, ids: tuple[str, ...], timeout_s: float) -> Query: ...
    def step(self, ids: tuple[str, ...], timeout_s: float) -> Query: ...
    def bmc(self, depth: int, timeout_s: float) -> Query: ...
    # An optional backend method may supply a second, separately counted query:
    # step_witness(ids, timeout_s) -> Query with formal source/next valuations.


class Proposer(Protocol):
    def propose(self, prompt: str, timeout_s: float) -> Reply: ...


@dataclass(frozen=True)
class Config:
    budget_s: float = 600.0
    bmc_depth: int = 30
    formal_timeout_s: float = 120.0
    llm_timeout_s: float = 120.0
    max_groups: int = 4
    max_group_size: int = 12
    max_additional_calls: int = 3
    max_selection_queries: int = 12
    initial_cost_s: dict[int, float] = field(default_factory=dict)
    step_cost_s: dict[int, float] = field(default_factory=dict)
    llm_estimate_s: float | None = None
    houdini_estimate_s: float | None = None
    formal_error_policy: str = "fail_fast"


@dataclass(frozen=True)
class Evidence:
    target: str | None  # None means the target property p.
    source: dict[str, bool | None]


@dataclass
class Result:
    status: str
    selected_ids: tuple[str, ...] = ()
    direct_status: str = "UNKNOWN"
    checked: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    reason: str = ""
    wall_s: float = 0.0
    formal_queries: int = 0
    selection_queries: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: dict | None = None
    reason_code: str = ""


class Selector:
    def __init__(
        self, rtl: str, property_text: str, candidates: list[str],
        backend: Backend, proposer: Proposer, config: Config = Config(),
    ):
        self.rtl = rtl
        self.property_text = property_text
        # Exact-text deduplication cannot change SVA semantics.
        unique = list(dict.fromkeys(candidates))
        width = max(2, len(str(len(unique))))
        self.pool = {f"L{i:0{width}d}": text for i, text in enumerate(unique, 1)}
        self.pool_hash = hashlib.sha256(
            json.dumps(list(self.pool.items()), ensure_ascii=False).encode()
        ).hexdigest()
        self.backend, self.proposer, self.config = backend, proposer, config
        if config.formal_error_policy != "fail_fast":
            raise ValueError("Only formal_error_policy='fail_fast' is supported")
        self.result = Result(status="UNKNOWN")
        self.started = 0.0
        self.disabled: set[str] = set()
        self.seen: set[tuple[str, ...]] = set()
        self.queue: list[tuple[str, ...]] = []
        self.evidence: list[Evidence] = []
        self.rank: list[str] = list(self.pool)
        self.last_failure: dict = {}
        self.stagnant_calls = 0
        self.cost_initial = dict(config.initial_cost_s)
        self.cost_step = dict(config.step_cost_s)
        self.cost_llm = config.llm_estimate_s or config.llm_timeout_s / 2
        self.target_initial_proven = False

    def _left(self) -> float:
        return max(0.0, self.config.budget_s - (time.monotonic() - self.started))

    def _reserve(self) -> float:
        # Cold-start Houdini estimate; replace with frozen dev-set estimates later.
        return min(self.config.budget_s, max(
            2 * self.config.formal_timeout_s,
            self.config.houdini_estimate_s or 0.4 * self.config.budget_s,
        ))

    def _event(self, kind: str, **fields) -> None:
        self.result.events.append({
            "at_s": round(time.monotonic() - self.started, 6),
            "kind": kind, **fields,
        })

    def _bucket(self, ids: tuple[str, ...]) -> int:
        return min(len(ids), 8)

    def _predict(self, ids: tuple[str, ...]) -> float:
        b = self._bucket(ids)
        cold = self.config.formal_timeout_s / 2
        return self.cost_initial.get(b, cold) + self.cost_step.get(b, cold)

    def _predict_typical(self) -> float:
        buckets = set(self.cost_initial) | set(self.cost_step)
        if not buckets:
            return self.config.formal_timeout_s
        cold = self.config.formal_timeout_s / 2
        return statistics.median(self.cost_initial.get(b, cold) +
                                 self.cost_step.get(b, cold) for b in buckets)

    def _query(self, phase: str, ids: tuple[str, ...] = (), *,
               selection: bool = False, reserve: bool = False,
               keep_s: float = 0.0) -> Query:
        if selection and self.result.selection_queries >= self.config.max_selection_queries:
            return Query(Status.UNKNOWN, detail="selection query limit", executed=False)
        allowance = self._left() - max(self._reserve() if reserve else 0.0, keep_s)
        cap = min(self.config.formal_timeout_s, allowance)
        if cap <= 0:
            return Query(Status.TIMEOUT, detail="wall budget or reserved time", executed=False)
        predicted = self._predict(ids) if phase in ("initial", "step") else None
        began = time.monotonic()
        try:
            if phase == "bmc":
                answer = self.backend.bmc(self.config.bmc_depth, cap)
            else:
                answer = getattr(self.backend, phase)(ids, cap)
        except FormalQueryError as exc:
            answer = exc.answer
        except Exception as exc:
            answer = Query(Status.ERROR, detail=str(exc), executed=False)
        actual = time.monotonic() - began
        if answer.elapsed_s <= 0:
            answer.elapsed_s = actual
        if answer.executed:
            self.result.formal_queries += 1
        if selection and answer.executed:
            self.result.selection_queries += 1
        if selection and answer.executed and phase in ("initial", "step"):
            bucket = self._bucket(ids)
            table = self.cost_initial if phase == "initial" else self.cost_step
            table[bucket] = 0.5 * answer.elapsed_s + 0.5 * table.get(
                bucket, self.config.formal_timeout_s / 2
            )
        self._event("formal", phase=phase, ids=list(ids), status=answer.status.value,
                    predicted_s=predicted, cap_s=cap,
                    elapsed_s=answer.elapsed_s, detail=answer.detail,
                    backend_log=answer.log, query_phase=answer.phase,
                    executed=answer.executed,
                    source_values=answer.source, next_values=answer.next_state,
                    p_next=answer.p_next, returncode=answer.returncode,
                    reason_code=answer.reason_code)
        if answer.status == Status.ERROR:
            raise FormalQueryError(phase, ids, answer)
        return answer

    @staticmethod
    def _parse_reply(text: str, valid_ids: list[str], max_groups: int,
                     max_size: int) -> tuple[list[str], list[tuple[str, ...]]]:
        payload = text.strip()
        if payload.startswith("<json>") and payload.endswith("</json>"):
            payload = payload[6:-7].strip()
        data = json.loads(payload)
        if not isinstance(data, dict) or not isinstance(data.get("ranked_ids"), list):
            raise ValueError("ranked_ids must be a list")
        if not isinstance(data.get("groups"), list) or len(data["groups"]) > max_groups:
            raise ValueError("groups must be a list within max_groups")
        ranked = data["ranked_ids"]
        valid_set = set(valid_ids)
        if any(not isinstance(x, str) or x not in valid_set for x in ranked):
            raise ValueError("ranking contains an unknown candidate ID")
        ranking = list(dict.fromkeys(ranked))
        ranking += [x for x in valid_ids if x not in ranking]
        groups = []
        for group in data["groups"]:
            ids = group.get("ids") if isinstance(group, dict) else None
            if (not isinstance(ids, list) or not 1 <= len(ids) <= max_size
                    or any(not isinstance(x, str) or x not in valid_set for x in ids)
                    or len(ids) != len(set(ids))):
                raise ValueError("invalid candidate group")
            key = tuple(sorted(ids))
            if key not in groups:
                groups.append(key)
        if not groups:
            raise ValueError("no valid group")
        return ranking, groups

    def _prompt(self, repair: bool) -> str:
        available = {key: text for key, text in self.pool.items()
                     if key not in self.disabled}
        payload = {
            "prompt_version": PROMPT_VERSION,
            "task": "Choose existing candidate IDs that jointly make p inductive.",
            "rules": ["Do not create or edit SVA or RTL.",
                      "Return only JSON with ranked_ids and groups of {ids, reason}.",
                      "Reasons never establish a proof."],
            "max_groups": self.config.max_groups,
            "max_group_size": min(self.config.max_group_size, len(available)),
            "rtl": self.rtl, "target_property": self.property_text,
            "candidates": available,
        }
        if repair:
            payload["feedback"] = {
                "last_failure": self.last_failure,
                "checked": self.result.checked[-16:],
                "disabled_ids": sorted(self.disabled),
                "remaining_wall_s": round(self._left(), 2),
                "remaining_selection_queries": (
                    self.config.max_selection_queries - self.result.selection_queries
                ),
            }
        return json.dumps(payload, ensure_ascii=False)

    def _ask(self, repair: bool) -> bool:
        available = [key for key in self.pool if key not in self.disabled]
        if not available:
            return False
        for attempt in range(2):
            limit = 1 + self.config.max_additional_calls
            if self.result.llm_calls >= limit:
                break
            spendable = self._left() - self._reserve()
            if self.cost_llm + self._predict_typical() > spendable:
                break
            cap = min(self.config.llm_timeout_s, spendable)
            began = time.monotonic()
            elapsed = 0.0
            self.result.llm_calls += 1
            try:
                reply = self.proposer.propose(self._prompt(repair), cap)
                self.result.input_tokens += reply.input_tokens
                self.result.output_tokens += reply.output_tokens
                elapsed = reply.elapsed_s or time.monotonic() - began
                ranking, groups = self._parse_reply(
                    reply.text, available, self.config.max_groups,
                    min(self.config.max_group_size, len(available)),
                )
            except Exception as exc:
                if (
                    getattr(exc, "status_code", None) == 503
                    or "API_KEY_MODEL_GROUPS_UNAVAILABLE" in str(exc)
                    or exc.__class__.__name__ == "APIConnectionError"
                ):
                    raise RuntimeError(
                        "LLM provider unavailable during GLS selection; "
                        "aborting instead of treating it as an invalid proposal"
                    ) from exc
                elapsed = elapsed or time.monotonic() - began
                self.cost_llm = 0.5 * elapsed + 0.5 * self.cost_llm
                self._event("llm_invalid", repair=repair, attempt=attempt,
                            detail=str(exc), elapsed_s=elapsed)
                continue
            self.cost_llm = 0.5 * elapsed + 0.5 * self.cost_llm
            self.rank = ranking
            added = self._enqueue(groups)
            self._event("llm", repair=repair, attempt=attempt, groups=[list(g) for g in groups],
                        added=added, elapsed_s=elapsed,
                        input_tokens=reply.input_tokens, output_tokens=reply.output_tokens)
            self.stagnant_calls = 0 if added else self.stagnant_calls + 1
            return bool(added)
        self.stagnant_calls += 1
        return False

    def _assessment(self, ids: tuple[str, ...]) -> tuple[bool, float]:
        relevant = [e for e in self.evidence if e.target is None or e.target in ids]
        covered = 0
        for e in relevant:
            values = [e.source.get(x) for x in ids]
            if False in values:
                covered += 1
            elif all(value is True for value in values):
                return False, 0.0
        return True, covered / len(relevant) if relevant else 0.0

    def _enqueue(self, groups: list[tuple[str, ...]]) -> int:
        added = 0
        for ids in groups:
            if (not ids or len(ids) > min(self.config.max_group_size, len(self.pool))
                    or set(ids) & self.disabled or ids in self.seen or ids in self.queue):
                continue
            if self._assessment(ids)[0]:
                self.queue.append(ids)
                added += 1
        return added

    def _record_evidence(self, ids: tuple[str, ...], answer: Query) -> list[Evidence]:
        if answer.status != Status.SAT or answer.source is None:
            return []
        items = []
        if answer.p_next is False:
            items.append(Evidence(None, answer.source))
        for candidate in ids:
            if (answer.next_state or {}).get(candidate) is False:
                items.append(Evidence(candidate, answer.source))
        self.evidence.extend(items)
        return items

    def _with_witness(self, ids: tuple[str, ...], answer: Query, *,
                      selection: bool, keep_s: float = 0.0) -> Query:
        if (answer.status != Status.SAT or answer.source is not None
                or not hasattr(self.backend, "step_witness")):
            return answer
        witness = self._query("step_witness", ids, selection=selection,
                              reserve=selection, keep_s=keep_s)
        if witness.status == Status.SAT and witness.source is not None:
            return witness
        answer.detail += "; witness: " + witness.detail
        answer.reason_code = witness.reason_code
        return answer

    def _local_repairs(self, ids: tuple[str, ...], items: list[Evidence]) -> None:
        suggestions = []
        for e in items:
            if e.target is not None:
                suggestions.append(tuple(x for x in ids if x != e.target))
            blockers = [x for x in self.rank if x not in ids and x not in self.disabled
                        and e.source.get(x) is False]
            suggestions.extend(tuple(sorted((*ids, x))) for x in blockers)
        self._enqueue(suggestions[:self.config.max_groups])

    def _candidate_refutation(self, candidate: str, *, selection: bool,
                              keep_s: float = 0.0) -> Query:
        # Prefer a candidate-only reachable-state query. A singleton initial
        # query still asserts p AND the candidate, so it isolates the candidate
        # only when p was independently proved initial.
        if hasattr(self.backend, "reachable"):
            return self._query("reachable", (candidate,), selection=selection,
                               reserve=selection, keep_s=keep_s)
        if self.target_initial_proven:
            return self._query("initial", (candidate,), selection=selection,
                               reserve=selection, keep_s=keep_s)
        return Query(Status.UNKNOWN, detail="target initiality is unproved",
                     executed=False)

    def _check(self, ids: tuple[str, ...]) -> tuple[str, Query]:
        self.seen.add(ids)
        initial = self._query("initial", ids, selection=True, reserve=True)
        if initial.status == Status.SAT:
            if initial.p_initial is False:
                return "UNSAFE", initial
            # Independent initial-state queries are the only basis for global disabling.
            for candidate in ids:
                if self.result.selection_queries >= self.config.max_selection_queries:
                    break
                single = self._candidate_refutation(candidate, selection=True)
                if single.status == Status.SAT:
                    self.disabled.add(candidate)
                    self._event("disable", id=candidate,
                                reason="independent candidate refutation")
            repaired = tuple(x for x in ids if x not in self.disabled)
            self._enqueue([repaired])
            return "INITIAL_SAT", initial
        if initial.status != Status.UNSAT:
            return initial.status.value, initial
        step = self._query("step", ids, selection=True, reserve=True)
        if step.status == Status.UNSAT:
            return "PROVED", step
        if step.status == Status.SAT:
            step = self._with_witness(ids, step, selection=True)
            items = self._record_evidence(ids, step)
            self._local_repairs(ids, items)
            return "STEP_SAT", step
        return step.status.value, step

    def _prescreen_headroom(self) -> float:
        # Leave room for one estimated LLM proposal, its check, and fallback.
        return min(self.config.budget_s,
                   self._reserve() + self.cost_llm + self._predict_typical() + 1.0)

    def _houdini(self, remaining: tuple[str, ...], *,
                 prescreen: bool = False) -> tuple[tuple[str, ...], Result | None]:
        """Houdini fixed-point loop over *remaining*.

        Returns the surviving candidate tuple paired with either a finished
        Result when a definitive answer is reached, or None when the loop
        exhausts without proving the goal (the caller decides what to do).
        """
        def query(phase: str, ids: tuple[str, ...]) -> Query:
            return self._query(phase, ids,
                               keep_s=self._prescreen_headroom() if prescreen else 0.0)

        def unresolved(answer: Query, phase: str) -> tuple[tuple[str, ...], Result | None]:
            if prescreen:
                self._event("houdini_prescreen_unresolved", phase=phase,
                            status=answer.status.value, detail=answer.detail,
                            remaining_ids=list(remaining))
                return remaining, None
            self.result.reason = answer.detail or "formal query did not resolve the obligation"
            self.result.reason_code = answer.reason_code or (
                "query_timeout" if answer.status == Status.TIMEOUT else "backend_inconclusive")
            return remaining, self._finish(answer.status.value)

        while self._left() > 0:
            if prescreen and self._left() <= self._prescreen_headroom():
                self._event("houdini_prescreen_reserved", remaining_ids=list(remaining))
                return remaining, None
            initial = query("initial", remaining)
            if initial.status == Status.SAT:
                if initial.p_initial is False:
                    return remaining, self._finish("UNSAFE")
                removed = set()
                for candidate in remaining:
                    single = self._candidate_refutation(
                        candidate, selection=False,
                        keep_s=self._prescreen_headroom() if prescreen else 0.0)
                    if single.status == Status.SAT:
                        removed.add(candidate)
                    elif single.status not in (Status.UNSAT, Status.SAT):
                        return unresolved(single, "reachable")
                if not removed:
                    return remaining, None
                self.disabled.update(removed)
                remaining = tuple(x for x in remaining if x not in removed)
                continue
            if initial.status != Status.UNSAT:
                return unresolved(initial, "initial")
            step = query("step", remaining)
            if step.status == Status.UNSAT:
                return remaining, self._finish("PROVED", remaining)
            if (step.phase == "induction_base" and
                    hasattr(self.backend, "reachable")):
                removed = set()
                for candidate in remaining:
                    bounded = query("reachable", (candidate,))
                    if bounded.status == Status.SAT:
                        removed.add(candidate)
                    elif bounded.status != Status.UNSAT:
                        return unresolved(bounded, "reachable")
                if not removed:
                    return remaining, None
                remaining = tuple(x for x in remaining if x not in removed)
                self._event("houdini_remove", ids=sorted(removed),
                            reason="independently refuted on a reachable state")
                continue
            if step.status != Status.SAT:
                return unresolved(step, "step")
            step = self._with_witness(
                remaining, step, selection=False,
                keep_s=self._prescreen_headroom() if prescreen else 0.0)
            # Houdini deletion requires a sound next-state valuation.
            if step.next_state is None:
                # Native SVA obligations preserve temporal syntax and only
                # supply positively refuted candidate IDs, not invented state
                # valuations for the selector's cross-combination pruning.
                removed = set()
                if hasattr(self.backend, "candidate_obligation"):
                    for candidate in remaining:
                        obligation = query("candidate_obligation", (candidate, *remaining))
                        if obligation.status == Status.SAT:
                            removed.add(candidate)
                        elif obligation.status in (Status.ERROR, Status.TIMEOUT):
                            return unresolved(obligation, "candidate_obligation")
                        else:
                            step.detail = obligation.detail
                            step.reason_code = obligation.reason_code
                if removed:
                    remaining = tuple(x for x in remaining if x not in removed)
                    self._event("houdini_remove", ids=sorted(removed),
                                reason="native candidate induction obligation refuted")
                    continue
                if prescreen:
                    self._event("houdini_prescreen_unresolved", phase="step_witness",
                                status="UNKNOWN", detail=step.detail,
                                reason_code=step.reason_code,
                                remaining_ids=list(remaining))
                else:
                    self.result.reason = "backend supplied no induction-step witness: " + step.detail
                    self.result.reason_code = step.reason_code or "incomplete_deletion_witness"
                return remaining, None
            removed = {x for x in remaining if step.next_state.get(x) is False}
            if removed:
                remaining = tuple(x for x in remaining if x not in removed)
                self._event("houdini_remove", ids=sorted(removed))
                continue
            if step.p_next is False:
                return remaining, self._finish("NO_PROOF_IN_POOL")
            return remaining, None
        if prescreen:
            return remaining, None
        return remaining, self._finish("TIMEOUT")

    def _fallback(self, reason: str) -> Result:
        self.result.reason = reason
        self._event("fallback", reason=reason)
        remaining = tuple(x for x in self.pool if x not in self.disabled)
        _, result = self._houdini(remaining)
        if result is not None:
            return result
        return self._finish("UNKNOWN")

    def _finish(self, status: str, ids: tuple[str, ...] = ()) -> Result:
        self.result.status = status
        if status == "PROVED":
            self.result.reason_code = ""
        elif status == "ERROR":
            self.result.reason_code = self.result.reason_code or "tool_error"
        elif status == "TIMEOUT":
            self.result.reason_code = self.result.reason_code or "task_budget_or_query_timeout"
        elif status == "UNKNOWN":
            self.result.reason_code = self.result.reason_code or "search_inconclusive"
        self.result.selected_ids = ids
        self.result.wall_s = time.monotonic() - self.started
        self._event("finish", status=status, ids=list(ids), reason=self.result.reason)
        return self.result

    def run(self) -> Result:
        self.started = time.monotonic()
        try:
            return self._run()
        except FormalQueryError as exc:
            self.result.error = exc.as_dict()
            self.result.reason = str(exc)
            self._event("formal_error", **self.result.error)
            return self._finish("ERROR")

    def _run(self) -> Result:
        initial = self._query("initial")
        if initial.status == Status.SAT:
            self.result.direct_status = "UNSAFE" if initial.p_initial is False else "UNKNOWN"
            if initial.p_initial is False:
                return self._finish("UNSAFE")
        elif initial.status == Status.UNSAT:
            self.target_initial_proven = True
            step = self._query("step")
            if step.status == Status.UNSAT:
                self.result.direct_status = "PROVED"
                return self._finish("PROVED")
            self.result.direct_status = "INCONCLUSIVE"
        else:
            self.result.direct_status = initial.status.value
            if initial.status == Status.ERROR:
                return self._finish("ERROR")
        bmc = self._query("bmc")
        if bmc.status == Status.SAT:
            return self._finish("UNSAFE")
        if bmc.status == Status.ERROR:
            return self._finish("ERROR")
        if not self.pool:
            return self._fallback("empty candidate pool")
        # Pre-screen: run Houdini over the full candidate pool before calling the
        # LLM.  If the pool is already an inductive fixed point this proves the
        # goal without any LLM call; otherwise the surviving (pruned) set is
        # passed to the LLM so it reasons over a smaller, cleaner candidate space.
        remaining = tuple(x for x in self.pool if x not in self.disabled)
        self._event("houdini_prescreen", candidates=list(remaining))
        remaining, early = self._houdini(remaining, prescreen=True)
        if early is not None:
            return early
        # Disable every candidate that Houdini pruned so _ask / _check see the
        # reduced pool, but do not permanently remove them from self.pool so the
        # original pool identity and cache keys remain intact.
        houdini_pruned = {x for x in self.pool if x not in remaining and x not in self.disabled}
        self.disabled.update(houdini_pruned)
        if houdini_pruned:
            self._event("houdini_prescreen_pruned", ids=sorted(houdini_pruned))
        if len(self.disabled) == len(self.pool):
            self.result.reason = "prescreen left no viable candidates"
            return self._finish("UNKNOWN")
        if not self._ask(repair=False):
            return self._fallback("initial LLM unavailable or invalid")
        while self._left() > 0:
            filtered = []
            for ids in self.queue:
                viable = self._assessment(ids)[0]
                if not viable:
                    self._event("pruned", ids=list(ids),
                                reason="known induction-step witness")
                elif not set(ids) & self.disabled and ids not in self.seen:
                    filtered.append(ids)
            self.queue = filtered
            spendable = self._left() - self._reserve()
            affordable = [x for x in self.queue if self._predict(x) <= spendable]
            if affordable and self.result.selection_queries + 2 <= self.config.max_selection_queries:
                position = {x: i for i, x in enumerate(self.queue)}
                def priority(ids):
                    _, coverage = self._assessment(ids)
                    order = 1 / (1 + position[ids])
                    predicted = self._predict(ids)
                    return (-(1 + coverage + order) / max(1.0, predicted),
                            predicted, len(ids), ids)
                ids = min(affordable, key=priority)
                self.queue.remove(ids)
                status, answer = self._check(ids)
                self.result.checked.append({"ids": list(ids), "status": status})
                self.last_failure = {"ids": list(ids), "status": status,
                                     "detail": answer.detail,
                                     "p_next": answer.p_next,
                                     "failed_lemmas": [x for x in ids if
                                                       (answer.next_state or {}).get(x) is False],
                                     "known_blockers": [x for x in self.pool if
                                                        (answer.source or {}).get(x) is False]}
                if status == "PROVED":
                    return self._finish("PROVED", ids)
                if status == "UNSAFE":
                    return self._finish("UNSAFE")
                continue
            if self.queue:
                return self._fallback("no affordable queued group")
            if (self.result.llm_calls < 1 + self.config.max_additional_calls
                    and self.stagnant_calls < 2
                    and self.result.selection_queries + 2 <= self.config.max_selection_queries):
                if self._ask(repair=True):
                    continue
            return self._fallback("no admissible repair request")
        return self._finish("TIMEOUT")
