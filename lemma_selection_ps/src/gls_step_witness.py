"""Obtain EBMC step witnesses for conservatively recognized state predicates.

The proof oracle remains EBMC k-induction. This additional bounded query is
used only after that oracle reports an induction-step SAT. Unsupported SVA
shapes return UNKNOWN rather than guessing their sampled semantics.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from gls import Query, Status
from formal_sva import (PropertyResolutionError, code_only, label_obligation,
                        property_rows, run_obligation)


def _execute(evaluator, command, timeout_s):
    if hasattr(evaluator, "execute_command"):
        return evaluator.execute_command(command, timeout_s)
    return subprocess.run(command, capture_output=True, text=True,
                          errors="replace", timeout=timeout_s)


_DECLARATION = re.compile(
    r"\s*property\s+[A-Za-z_]\w*\s*;(?P<body>.*?)endproperty\s*",
    re.DOTALL,
)
_CLOCK = re.compile(r"^@\s*\(\s*(posedge|negedge)\s+([A-Za-z_]\w*)\s*\)")
_DISABLE = re.compile(r"^disable\s+iff\s*\(")
_TEMPORAL = re.compile(
    r"\|->|\|=>|##|\[\*|\[->|\[=|\$|\b(?:always|s_always|eventually|"
    r"s_eventually|until|until_with|s_until|s_until_with|throughout|within|"
    r"intersect|first_match|strong|weak|nexttime|s_nexttime|implies|iff|"
    r"accept_on|reject_on|sync_accept_on|sync_reject_on|and|or|not)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StatePredicate:
    expression: str
    clock: tuple[str, str] | None
    disable: str | None

    @property
    def sampling(self) -> tuple[tuple[str, str] | None, str | None]:
        return self.clock, re.sub(r"\s+", "", self.disable) if self.disable else None


def parse_state_predicate(text: str) -> StatePredicate | None:
    """Recognize only a single-cycle Boolean SVA property body."""
    raw = text.strip()
    if raw.startswith("property"):
        match = _DECLARATION.fullmatch(raw)
        if not match:
            return None
        raw = match.group("body").strip()
    raw = raw.removesuffix(";").strip()
    clock = None
    if raw.startswith("@"):
        match = _CLOCK.match(raw)
        if not match:
            return None
        clock = match.group(1), match.group(2)
        raw = raw[match.end():].strip()
    disable = None
    match = _DISABLE.match(raw)
    if match:
        depth = 1
        index = match.end()
        start = index
        while index < len(raw) and depth:
            if raw[index] == "(":
                depth += 1
            elif raw[index] == ")":
                depth -= 1
            index += 1
        if depth:
            return None
        disable = raw[start:index - 1].strip()
        raw = raw[index:].strip()
        if not disable or _TEMPORAL.search(disable):
            return None
    raw = _state_expression(raw)
    if raw is None:
        return None
    return StatePredicate(raw, clock, disable)


def _state_expression(raw: str) -> str | None:
    """Normalize Boolean overlapped implication, never a delayed sequence."""
    raw = raw.strip()
    while raw.startswith("(") and raw.endswith(")"):
        depth = 0
        for i, char in enumerate(raw):
            depth += (char == "(") - (char == ")")
            if depth == 0:
                break
        if i != len(raw) - 1:
            break
        raw = raw[1:-1].strip()
    depth = 0
    for i, char in enumerate(raw):
        depth += (char == "(") - (char == ")")
        if depth == 0 and raw[i:i + 3] == "|->":
            left, right = _state_expression(raw[:i]), _state_expression(raw[i + 3:])
            return f"(!({left}) || ({right}))" if left and right else None
    if not raw or ";" in raw or "@" in raw or _TEMPORAL.search(raw):
        return None
    return raw


def _probe_value(trace: str, name: str, state: int) -> bool | None:
    marker = re.search(rf"^Transition system state {state}\s*$", trace, re.MULTILINE)
    if not marker:
        return None
    end = re.search(r"^Transition system state \d+\s*$", trace[marker.end():],
                    re.MULTILINE)
    block = trace[marker.end():marker.end() + end.start()] if end else trace[marker.end():]
    match = re.search(rf"^\s*(?:\S+\.)?{re.escape(name)}\s*=\s*(\S+)",
                      block, re.MULTILINE)
    if not match:
        return None
    token = match.group(1).lower()
    if token in {"1", "1'b1", "1'h1", "1'd1", "true"}:
        return True
    if token in {"0", "1'b0", "1'h0", "1'd0", "false"}:
        return False
    return None


def find_step_witness(evaluator, pool: dict[str, str], ids: tuple[str, ...],
                      timeout_s: float, *, candidates_only: bool = False) -> Query:
    selected = {candidate: parse_state_predicate(pool[candidate]) for candidate in ids}
    if candidates_only:
        first = next((spec for spec in selected.values() if spec is not None), None)
        target = StatePredicate("1'b1", first.clock, first.disable) if first else None
    else:
        target = parse_state_predicate(evaluator.module.get_property())
    if target is None or any(spec is None for spec in selected.values()):
        return Query(Status.UNKNOWN, detail="temporal or unsupported SVA property",
                     executed=False, reason_code="state_witness_unavailable")
    if any(spec.sampling != target.sampling for spec in selected.values()):
        return Query(Status.UNKNOWN, detail="candidate sampling differs from target",
                     executed=False, reason_code="state_witness_sampling_mismatch")

    supported = {candidate: spec for candidate, text in pool.items()
                 if (spec := parse_state_predicate(text)) is not None
                 and spec.sampling == target.sampling}
    source_text = "".join(evaluator.module.lines)
    nonce = hashlib.sha256((source_text + repr(pool)).encode()).hexdigest()[:10]
    while f"gls_probe_{nonce}_" in source_text:
        nonce += "x"
    probe = lambda candidate: f"gls_probe_{nonce}_{candidate}"
    names = {"p": probe("p"), **{candidate: probe(candidate) for candidate in supported}}
    declarations = [f"wire {names['p']} = ({target.expression});\n"]
    declarations.extend(
        f"wire {names[candidate]} = ({spec.expression});\n"
        for candidate, spec in supported.items()
    )
    reset_probe = None
    if target.disable:
        reset_probe = probe("disable")
        declarations.append(f"wire {reset_probe} = ({target.disable});\n")
    invariant = " && ".join([names["p"], *(names[x] for x in ids)])
    prefix = ""
    if target.clock:
        prefix += f"@({target.clock[0]} {target.clock[1]}) "
    if target.disable:
        prefix += f"disable iff ({target.disable}) "
    declarations.append(f"assert property({prefix}(({invariant}) |=> ({invariant})));\n")
    lines = evaluator.module.lines[:]
    declarations, property_label = label_obligation(
        lines, evaluator.module._initial_insert_at, declarations)
    lines[evaluator.module._initial_insert_at:evaluator.module._initial_insert_at] = declarations

    began = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix=".gls_step_",
                                         dir=Path(evaluator.module.filepath).parent) as folder:
            path = Path(folder) / Path(evaluator.module.filepath).name
            path.write_text("".join(lines), encoding="utf-8")
            # k-induction's step has an arbitrary source state. --ignore-initial
            # gives the bounded implication that same arbitrary source. Do not
            # add --reset: it constrains the bounded source to a reset state.
            command = [evaluator.ebmc_path, str(path), "--ignore-initial",
                       "--bound", "1", "--trace"]
            completed, property_id = run_obligation(
                command, property_label, timeout_s,
                execute=lambda args, cap: _execute(evaluator, args, cap))
    except PropertyResolutionError as exc:
        return Query(Status.ERROR, time.monotonic() - began, detail=str(exc),
                     log=exc.log, returncode=exc.returncode, reason_code=exc.reason_code)
    except subprocess.TimeoutExpired:
        return Query(Status.TIMEOUT, time.monotonic() - began,
                     detail="step witness query timed out")
    except OSError as exc:
        return Query(Status.ERROR, time.monotonic() - began, detail=str(exc),
                     executed=False)
    elapsed = time.monotonic() - began
    output = completed.stdout
    if (completed.returncode not in (0, 10) or
            re.search(r"^\s*error:|invariant violation report", output + completed.stderr,
                      re.MULTILINE | re.IGNORECASE)):
        return Query(Status.ERROR, elapsed, detail=(output + completed.stderr)[-2000:],
                     log=output + completed.stderr, returncode=completed.returncode)
    if "REFUTED" not in property_rows(output, property_id):
        return Query(Status.UNKNOWN, elapsed,
                     detail="bounded step has no validated counterexample",
                     log=output + completed.stderr)
    if "Transition system state 0" not in output or "Transition system state 1" not in output:
        return Query(Status.UNKNOWN, elapsed, detail="step trace is incomplete", log=output)

    source = {candidate: _probe_value(output, name, 0)
              for candidate, name in names.items() if candidate != "p"}
    next_state = {candidate: _probe_value(output, name, 1)
                  for candidate, name in names.items() if candidate != "p"}
    source = {candidate: source.get(candidate) for candidate in pool}
    next_state = {candidate: next_state.get(candidate) for candidate in pool}
    p_source = _probe_value(output, names["p"], 0)
    p_next = _probe_value(output, names["p"], 1)
    if (p_source is not True or any(source[x] is not True for x in ids)
            or (reset_probe is not None and
                (_probe_value(output, reset_probe, 0) is not False or
                 _probe_value(output, reset_probe, 1) is not False))
            or not (p_next is False or any(next_state[x] is False for x in ids))):
        return Query(Status.UNKNOWN, elapsed,
                     detail="probe values do not establish a joint step failure",
                     log=output)
    return Query(Status.SAT, elapsed, detail="bounded arbitrary-state step witness",
                 log=output, source=source, next_state=next_state, p_next=p_next)


def check_candidate_obligation(evaluator, pool: dict[str, str], ids: tuple[str, ...],
                               candidate: str, timeout_s: float, *,
                               candidates_only: bool = False) -> Query:
    """Ask EBMC for H(source) => candidate(next), retaining native SVA.

    The fresh arbitrary flag can be false only at the source frame: its next
    value is unconditionally true. Candidate assumptions apply ONLY at source,
    not at the successor (which would make the obligation vacuous). Native SVA
    handles delays, past operators and disable conditions. A bounded UNSAT is
    only an absence of a deletion witness, NEVER an inductive proof/fixed point.
    No Boolean source valuations are fabricated for temporal properties.
    """
    source = "".join(evaluator.module.lines)
    texts = [pool[key] for key in ids]
    if not candidates_only:
        texts.append(evaluator.module.get_property())
    clocks = set(re.findall(r"@\s*\(\s*(posedge|negedge)\s+([A-Za-z_]\w*)\s*\)",
                            code_only("\n".join(texts))))
    if not clocks:
        clocks = set(re.findall(r"\balways(?:_ff)?\s*@\s*\(\s*(posedge|negedge)\s+([A-Za-z_]\w*)",
                                code_only("".join(evaluator.module.lines[:evaluator.module._initial_insert_at]))))
    if len(clocks) != 1:
        return Query(Status.UNKNOWN, executed=False,
                     detail="native step obligation requires one resolved sampling clock",
                     reason_code="unsupported_sampling")
    edge, clock = next(iter(clocks))
    nonce = hashlib.sha256((source + repr(pool)).encode()).hexdigest()[:12]
    prefix = "gls_obligation_" + nonce
    while prefix in source:
        prefix += "x"
    flag = prefix + "_after_source"
    names = {key: prefix + "_" + key for key in ids}
    declarations = [f"reg {flag};\nalways @({edge} {clock}) {flag} <= 1'b1;\n"]
    for key in ids:
        declaration, count = re.subn(r"\bproperty\s+[A-Za-z_]\w*", "property " + names[key],
                                     pool[key], count=1)
        if count != 1:
            return Query(Status.ERROR, executed=False,
                         detail="candidate is not a named property declaration: " + key,
                         reason_code="malformed_candidate")
        declarations.append(declaration + "\n")
    hypotheses = [names[key] for key in ids]
    if not candidates_only:
        hypotheses.insert(0, "prop")
    declarations += [f"assume property ((!{flag}) |-> ({name}));\n" for name in hypotheses]
    declarations.append(f"assert property ((!{flag}) |=> ({names[candidate]}));\n")
    lines = evaluator.module.lines[:]
    declarations, property_label = label_obligation(lines, evaluator.module._initial_insert_at,
                                                    declarations)
    lines[evaluator.module._initial_insert_at:evaluator.module._initial_insert_at] = declarations
    began = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix=".gls_obligation_",
                                         dir=Path(evaluator.module.filepath).parent) as folder:
            path = Path(folder) / Path(evaluator.module.filepath).name
            path.write_text("".join(lines), encoding="utf-8")
            completed, property_id = run_obligation(
                [evaluator.ebmc_path, str(path), "--ignore-initial", "--bound", "30", "--trace"],
                property_label, timeout_s,
                execute=lambda args, cap: _execute(evaluator, args, cap))
    except PropertyResolutionError as exc:
        return Query(Status.ERROR, time.monotonic() - began, detail=str(exc),
                     log=exc.log, returncode=exc.returncode, reason_code=exc.reason_code)
    except subprocess.TimeoutExpired:
        return Query(Status.TIMEOUT, time.monotonic() - began,
                     detail="native candidate obligation timed out", reason_code="query_timeout")
    except OSError as exc:
        return Query(Status.ERROR, time.monotonic() - began, detail=str(exc),
                     executed=False, reason_code="tool_error")
    elapsed = time.monotonic() - began
    output = completed.stdout + completed.stderr
    rows = property_rows(completed.stdout, property_id)
    common = dict(elapsed_s=elapsed, log=output, phase="native_candidate_obligation",
                  returncode=completed.returncode)
    if completed.returncode not in (0, 10) or re.search(
            r"^\s*error:|invariant violation report", output, re.MULTILINE | re.IGNORECASE):
        return Query(Status.ERROR, detail=output[-2000:], reason_code="tool_error", **common)
    if "REFUTED" in rows and _probe_value(completed.stdout, flag, 0) is False:
        return Query(Status.SAT, detail="native SVA candidate step obligation refuted",
                     next_state={candidate: False}, reason_code="candidate_step_refuted", **common)
    if rows and completed.returncode == 0 and all(row.startswith("PROVED") for row in rows):
        return Query(Status.UNSAT, detail="no candidate step counterexample within bound 30; not a proof",
                     reason_code="no_bounded_deletion_witness", **common)
    return Query(Status.UNKNOWN, detail="native obligation has no validated deletion witness",
                 reason_code="incomplete_deletion_witness", **common)
