"""Name and select one verification obligation without changing the design.

Original assertions remain in the source, but EBMC's --property option selects
only the generated obligation. Assumptions and property declarations are kept.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time


BACKEND_SEMANTICS_VERSION = "ebmc-selected-obligations-v5"


def code_only(text: str) -> str:
    """Mask comments/strings, preserving offsets and line numbers."""
    return re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                  lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def label_obligation(lines: list[str], insert_at: int,
                     directives: list[str]) -> tuple[list[str], str | None]:
    """Label one assertion; its elaborated instance ID is resolved by EBMC."""
    assertions = [i for i, line in enumerate(directives)
                  if re.match(r"\s*assert\s+property\b", line)]
    if len(assertions) != 1:
        return directives, None
    source = "".join(lines)
    label = "gls_query_" + hashlib.sha256(source.encode()).hexdigest()[:12]
    while re.search(rf"\b{label}\b", code_only(source)):
        label += "x"
    result = directives[:]
    i = assertions[0]
    result[i] = label + ": " + result[i].lstrip()
    return result, label


class PropertyResolutionError(RuntimeError):
    def __init__(self, message, completed, reason_code):
        super().__init__(message)
        self.log = completed.stdout + completed.stderr
        self.returncode = completed.returncode
        self.reason_code = reason_code


def resolve_property_id(completed, label: str) -> str:
    """Match the exact generated label in EBMC's elaborated property list.

    A module may have multiple instances. Until the caller specifies the intended
    instance, refusing an ambiguous match is safer than proving just one copy.
    """
    output = completed.stdout + completed.stderr
    if completed.returncode != 0 or re.search(
            r"^\s*error:|invariant violation report", output, re.MULTILINE | re.IGNORECASE):
        raise PropertyResolutionError(output[-2000:], completed, "property_discovery_failed")
    ids = list(dict.fromkeys(re.findall(r"^(\S+):[ \t]", completed.stdout, re.MULTILINE)))
    matches = [name for name in ids if name == label or name.endswith("." + label)]
    if not matches:
        raise PropertyResolutionError(
            f"Generated assertion {label} not found; discovered IDs: {ids}",
            completed, "property_not_found")
    if len(matches) != 1:
        raise PropertyResolutionError(
            f"Generated assertion {label} has multiple instances; explicit instance selection "
            f"is required: {matches}", completed, "ambiguous_property")
    return matches[0]


def run_obligation(command, label: str | None, timeout_s: float, *, execute=None):
    """Resolve and check one assertion within a shared per-query time budget.

    Both invocations use the same source and options. The experiment executor
    audits both processes and also enforces the overall task deadline. No path
    or proof cache is used, so discovery costs apply equally to all methods.
    """
    command = [str(value) for value in command]
    if "--property" in command or "--show-properties" in command:
        raise ValueError("run_obligation requires a command without property selection")
    if execute is None:
        def execute(args, cap):
            return subprocess.run(args, capture_output=True, text=True,
                                  errors="replace", timeout=cap)
    began = time.monotonic()
    property_id = None
    if label is not None:
        discovered = execute(command + ["--show-properties"], timeout_s)
        property_id = resolve_property_id(discovered, label)
        command += ["--property", property_id]
    remaining = timeout_s - (time.monotonic() - began)
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, timeout_s)
    return execute(command, remaining), property_id


def property_rows(output: str, expected_property: str | None = None) -> list[str]:
    rows = re.findall(
        r"^\[([^\n]+?)\][ \t]+.*: (PROVED up to bound \d+|PROVED|REFUTED|INCONCLUSIVE|UNKNOWN)\s*$",
        output, re.MULTILINE)
    return [status for name, status in rows
            if expected_property is None or name == expected_property]
