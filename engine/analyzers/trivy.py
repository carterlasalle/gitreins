"""R2.4 Trivy analyzer adapter — ``trivy fs --format json`` -> Evidence.

DESIGN_v2.md §6.1 / §16: Trivy joins the analyzer adapters. Every vulnerability
in ``data["Results"][*]["Vulnerabilities"]`` becomes one kind='static_analysis'
Evidence. ``file`` is the package path (``PkgPath``) when present, otherwise
the scan target. Returns ``[]`` (logged) when trivy is not installed.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

from engine.evidence.models import Evidence, make_ref

logger = logging.getLogger("gitreins.analyzers.trivy")

_SOURCE = "trivy"


def parse_trivy_json(data: dict) -> list[Evidence]:
    """Parse ``trivy fs --format json`` output into Evidence.

    Each vulnerability carries ``VulnerabilityID``, ``PkgName``,
    ``InstalledVersion``, ``FixedVersion``, ``Severity``, ``Title`` and
    optionally ``PkgPath``; the message is ``"<VulnerabilityID> <Title>"``.
    """
    evidence: list[Evidence] = []
    i = 0
    for result in data.get("Results") or []:
        target = result.get("Target", "")
        for vuln in result.get("Vulnerabilities") or []:
            i += 1
            vid = vuln.get("VulnerabilityID", "")
            title = vuln.get("Title", "")
            severity = vuln.get("Severity", "")
            if isinstance(severity, list):  # newer trivy may emit a list
                severity = ",".join(severity)
            payload = {
                "code": vid,
                "message": f"{vid} {title}".strip(),
                "severity": severity,
                "package": vuln.get("PkgName", ""),
                "installed_version": vuln.get("InstalledVersion", ""),
                "fixed_version": vuln.get("FixedVersion", ""),
                "tool": _SOURCE,
            }
            evidence.append(
                Evidence(
                    id=make_ref("static_analysis", _SOURCE, str(i)),
                    kind="static_analysis",
                    source=_SOURCE,
                    file=vuln.get("PkgPath") or target or None,
                    line_start=None,
                    line_end=None,
                    payload=payload,
                )
            )
    return evidence


def run_trivy(workdir: str, timeout: float = 300.0) -> list[Evidence]:
    """Run ``trivy fs --format json --quiet <workdir>`` and return Evidence.

    Returns ``[]`` (with a logged note) when trivy is not on PATH, the scan
    fails, or the output is not parseable JSON. Trivy exits 0 even when
    vulnerabilities are found, so stdout is parsed whenever present.
    """
    if shutil.which("trivy") is None:
        logger.warning("trivy not found on PATH — skipping fs scan (no evidence)")
        return []
    cmd = ["trivy", "fs", "--format", "json", "--quiet", workdir]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("trivy fs scan failed: %s", exc)
        return []
    if not result.stdout.strip():
        logger.info("trivy fs scan produced no output (rc=%s)", result.returncode)
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("trivy returned invalid JSON — no evidence")
        return []
    return parse_trivy_json(data)
