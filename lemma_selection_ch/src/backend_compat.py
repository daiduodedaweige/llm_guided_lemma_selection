"""Bounded, equivalent EBMC input lowering and compiler-error attribution."""

from __future__ import annotations

import re

from formal_sva import code_only


def lower_rank_reductions(source: str) -> tuple[str, list[dict]]:
    """Lower two known reductions on a single top's constant-size ranks array.

    Keep casts and bitwise reduction operators intact. Unsupported declarations,
    specializations and expressions are left to the compiler, never guessed.
    Newline counts are preserved so compiler diagnostics still map to candidates.
    """
    code = code_only(source)
    if len(re.findall(r"\bmodule\b", code)) != 1 or re.search(r"\bdefparam\b", code):
        return source, []
    sizes = re.findall(r"\bparameter\s+NUM_REQ\s*=\s*(\d+)\s*[,)]", code)
    if len(sizes) != 1 or not 1 <= int(sizes[0]) <= 128:
        return source, []
    if not re.search(r"\blogic\s*\[\s*NUM_REQ_W\s*-\s*1\s*:\s*0\s*\]"
                     r"\s+ranks\s*\[\s*NUM_REQ\s*-\s*1\s*:\s*0\s*\]\s*;", code):
        return source, []
    patterns = (
        ("or", re.compile(r"\branks\s*\.\s*or\s*\(\s*\)\s*with\s*\(\s*"
                          r"NUM_REQ\s*'\s*\(\s*(?:1|1'b1)\s*\)\s*<<\s*item\s*\)")),
        ("and", re.compile(r"\branks\s*\.\s*and\s*\(\s*\)\s*with\s*"
                           r"\(\s*item\s*<\s*NUM_REQ\s*\)")),
    )
    changes = []
    replacements = []
    for operation, pattern in patterns:
        for match in pattern.finditer(code):
            expression = source[match.start():match.end()]
            if operation == "or":
                cast = re.search(r"NUM_REQ\s*'\s*\(\s*(?:1|1'b1)\s*\)", match.group()).group()
                cast = re.sub(r"\s+", "", cast)
                terms = [f"({cast} << ranks[{i}])" for i in range(int(sizes[0]))]
                joiner = " | "
            else:
                terms = [f"(ranks[{i}] < NUM_REQ)" for i in range(int(sizes[0]))]
                joiner = " & "
            replacement = "(" + "\n" * expression.count("\n") + joiner.join(terms) + ")"
            replacements.append((match.start(), match.end(), replacement))
            changes.append({"kind": "rank_array_reduction", "operation": operation,
                            "elements": int(sizes[0]),
                            "line": source.count("\n", 0, match.start()) + 1})
    for start, end, replacement in sorted(replacements, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source, sorted(changes, key=lambda row: row["line"])


def candidate_error_id(log: str, source: str, renamed: dict[str, str]) -> str | None:
    """Attribute only syntax/unknown-name errors inside one generated declaration.

    RTL errors, assertion-site errors, crashes and ambiguous diagnostics remain
    fatal. The caller must separately verify the unmodified design compiles.
    """
    if re.search(r"invariant violation|precondition|backtrace", log, re.I):
        return None
    spans = []
    code = code_only(source)
    for name, candidate in renamed.items():
        match = re.search(rf"\bproperty\s+{re.escape(name)}\b.*?\bendproperty\b", code, re.S)
        if match:
            spans.append((source.count("\n", 0, match.start()) + 1,
                          source.count("\n", 0, match.end()) + 1, candidate))
    found = set()
    for match in re.finditer(r"\bline\s+(\d+):[^\n]*(?:syntax error|unknown identifier|unknown function)[^\n]*",
                             log, re.I):
        line = int(match.group(1))
        owners = {candidate for start, end, candidate in spans if start <= line <= end}
        if len(owners) != 1:
            return None
        found.update(owners)
    return next(iter(found)) if len(found) == 1 else None
