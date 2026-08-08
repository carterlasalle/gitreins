"""R2.4 gitleaks analyzer adapter — ``gitleaks detect --json`` -> Evidence.

DESIGN_v2.md §6.1 / §16: gitleaks joins the analyzer adapters. Secrets are
NEVER stored in evidence payloads: ``payload["secret"]`` is always the literal
``<redacted>`` so findings can be stored and cited without leaking credentials
(the Tier-1 secrets guard scans commits). The raw ``Secret`` / ``Match``
fields are dropped entirely. Returns ``[]`` (logged) when gitleaks is absent.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

from engine.evidence.models import Evidence, make_ref

logger = logging.getLogger("gitreins.analyzers.gitleaks")

_SOURCE = "gitleaks"
_REDACTED = "<redacted>"


def parse_gitleaks_json(data: dict | list) -> list[Evidence]:
    """Parse ``gitleaks detect --json`` output into Evidence.

    Accepts the JSON array of findings (the tool's native shape) or a dict
    with a ``findings`` key. Each finding carries ``RuleID``, ``Description``,
    ``File``, ``StartLine`` / ``EndLine`` and ``Severity``. The raw secret is
    redacted to ``<redacted>`` in the payload.
    """
    if isinstance(data, dict):
        findings = data.get("findings") or []
    else:
        findings = data or []
    evidence: list[Evidence] = []
    for i, finding in enumerate(findings, start=1):
        payload = {
            "code": finding.get("RuleID", ""),
            "message": finding.get("Description", ""),
            "severity": finding.get("Severity", ""),
            "secret": _REDACTED,
            "tool": _SOURCE,
        }
        evidence.append(
            Evidence(
                id=make_ref("static_analysis", _SOURCE, str(i)),
                kind="static_analysis",
                source=_SOURCE,
                file=finding.get("File"),
                line_start=finding.get("StartLine"),
                line_end=finding.get("EndLine"),
                payload=payload,
            )
        )
    return evidence


def run_gitleaks(workdir: str, timeout: float = 120.0) -> list[Evidence]:
    """Run ``gitleaks detect --source <workdir> --no-git --json``.

    Returns ``[]`` (with a logged note) when gitleaks is not on PATH, the
    scan fails, or the output is not parseable JSON. Gitleaks exits 1 when
    leaks are found, so stdout is parsed regardless of the exit code.
    """
    if shutil.which("gitleaks") is None:
        logger.warning("gitleaks not found on PATH — skipping secret scan (no evidence)")
        return []
    cmd = ["gitleaks", "detect", "--source", workdir, "--no-git", "--json"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("gitleaks detect failed: %s", exc)
        return []
    if not result.stdout.strip():
        logger.info("gitleaks detect produced no output (rc=%s)", result.returncode)
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("gitleaks returned invalid JSON — no evidence")
        return []
    return parse_gitleaks_json(data)
