"""R2.4 ast-grep analyzer adapter — ``ast-grep scan --json`` -> Evidence.

DESIGN_v2.md §6.1 / §16: ast-grep joins the analyzer adapters. This module is
standalone — it deliberately does NOT import ``engine/evaluator.py``'s
``_tool_scan_security`` (ast-grep against the CodeRabbit rules); that stays an
evaluator tool. The runner here invokes the stock ``ast-grep scan`` and turns
every match into kind='static_analysis' Evidence.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

from engine.evidence.models import Evidence, make_ref

logger = logging.getLogger("gitreins.analyzers.astgrep")

_SOURCE = "ast-grep"


def parse_astgrep_json(data: dict | list) -> list[Evidence]:
    """Parse ``ast-grep scan --json`` output into Evidence.

    Accepts the JSON array of matches (the tool's native shape) or a dict
    with a ``matches`` key. Each match carries ``text``, ``file``,
    ``range.start.line`` / ``range.end.line``, ``rule.id``, ``severity`` and
    ``message``. Line numbers are preserved as reported by the tool.
    """
    if isinstance(data, dict):
        matches = data.get("matches") or []
    else:
        matches = data or []
    evidence: list[Evidence] = []
    for i, match in enumerate(matches, start=1):
        rule = match.get("rule") or {}
        rule_id = rule.get("id") if isinstance(rule, dict) else str(rule)
        severity = match.get("severity") or (rule.get("severity") if isinstance(rule, dict) else "")
        rng = match.get("range") or {}
        payload = {
            "code": rule_id,
            "message": match.get("message", ""),
            "severity": severity,
            "tool": _SOURCE,
        }
        evidence.append(
            Evidence(
                id=make_ref("static_analysis", _SOURCE, str(i)),
                kind="static_analysis",
                source=_SOURCE,
                file=match.get("file"),
                line_start=rng.get("start", {}).get("line"),
                line_end=rng.get("end", {}).get("line"),
                payload=payload,
            )
        )
    return evidence


def run_astgrep(workdir: str, path: str | None = None, timeout: float = 120.0) -> list[Evidence]:
    """Run ``ast-grep scan --json`` in ``workdir`` and return Evidence.

    Returns ``[]`` (with a logged note) when ast-grep is not on PATH, the
    scan fails, or the output is not parseable JSON.
    """
    if shutil.which("ast-grep") is None:
        logger.warning("ast-grep not found on PATH — skipping scan (no evidence)")
        return []
    cmd = ["ast-grep", "scan", "--json", path or "."]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("ast-grep scan failed: %s", exc)
        return []
    if not result.stdout.strip():
        logger.info("ast-grep scan produced no output (rc=%s)", result.returncode)
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("ast-grep returned invalid JSON — no evidence")
        return []
    return parse_astgrep_json(data)
