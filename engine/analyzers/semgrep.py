"""R2.4 Semgrep analyzer adapter — ``semgrep scan --json`` -> Evidence.

DESIGN_v2.md §6.1 / §16: semgrep findings become evidence producers
("Semgrep warning -> Evidence E82 -> review agent considers it"). Each result
in ``data["results"]`` becomes one kind='static_analysis' Evidence with a
stable ordinal ref (``static:semgrep:1``) so review agents can cite findings
before they are stored. Returns ``[]`` (logged) when semgrep is not installed.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

from engine.evidence.models import Evidence, make_ref

logger = logging.getLogger("gitreins.analyzers.semgrep")

_SOURCE = "semgrep"


def parse_semgrep_json(data: dict) -> list[Evidence]:
    """Parse ``semgrep scan --json`` output into Evidence.

    Each item in ``data["results"]`` is a finding dict with ``check_id``,
    ``path``, ``start.line`` / ``end.line``, and ``extra.message`` /
    ``extra.severity`` (semgrep's native severity vocabulary: ERROR, WARNING,
    INFO). Line numbers are preserved as reported by the tool.
    """
    evidence: list[Evidence] = []
    for i, finding in enumerate(data.get("results") or [], start=1):
        extra = finding.get("extra") or {}
        payload = {
            "code": finding.get("check_id", ""),
            "message": extra.get("message", ""),
            "severity": extra.get("severity", ""),
            "tool": _SOURCE,
        }
        evidence.append(
            Evidence(
                id=make_ref("static_analysis", _SOURCE, str(i)),
                kind="static_analysis",
                source=_SOURCE,
                file=finding.get("path"),
                line_start=finding.get("start", {}).get("line"),
                line_end=finding.get("end", {}).get("line"),
                payload=payload,
            )
        )
    return evidence


def run_semgrep(workdir: str, path: str | None = None, timeout: float = 120.0) -> list[Evidence]:
    """Run ``semgrep scan --json`` in ``workdir`` and return Evidence.

    Returns ``[]`` (with a logged note) when semgrep is not on PATH, the scan
    fails, or the output is not parseable JSON. Semgrep exits 1 when rules
    match, so findings are parsed from stdout regardless of the exit code.
    """
    if shutil.which("semgrep") is None:
        logger.warning("semgrep not found on PATH — skipping scan (no evidence)")
        return []
    cmd = ["semgrep", "scan", "--json", path or "."]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("semgrep scan failed: %s", exc)
        return []
    if not result.stdout.strip():
        logger.info("semgrep scan produced no output (rc=%s)", result.returncode)
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("semgrep returned invalid JSON — no evidence")
        return []
    return parse_semgrep_json(data)
