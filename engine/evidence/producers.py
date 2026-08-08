"""R2.4 Evidence producers — wrap existing parsers as evidence (DESIGN_v2.md §6).

The normalized parsers in ``engine/static_analysis.py`` stay as-is; these
producers convert their output (``StaticDiag`` objects / the dicts returned by
``run_static_check``) into Evidence. Pipeline shape per the design:
"Semgrep warning -> Evidence E82 -> review agent considers it".

``from_command`` produces kind='command' evidence for future verifier stages.
"""

from __future__ import annotations

import importlib
import logging

from engine.evidence.models import Evidence, make_ref
from engine.static_analysis import StaticDiag

logger = logging.getLogger("gitreins.evidence.producers")


def from_static_diag(
    diag: StaticDiag, tool: str | None = None, *, ordinal: int | None = None
) -> Evidence:
    """Wrap a ``StaticDiag`` as kind='static_analysis' Evidence.

    ``tool`` overrides the source (defaults to ``diag.tool``). When ``ordinal``
    is given, the id becomes a stable tool ref (``static:semgrep:17``);
    otherwise the id is left empty for the store to auto-assign (``E<n>``).
    """
    tool_name = tool or diag.tool or "unknown"
    payload = {
        "code": diag.code,
        "message": diag.message,
        "severity": diag.severity,
        "tool": tool_name,
    }
    id = ""
    if ordinal is not None:
        id = make_ref("static_analysis", tool_name, str(ordinal))
    return Evidence(
        id=id,
        kind="static_analysis",
        source=tool_name,
        file=diag.file,
        line_start=diag.line,
        line_end=diag.line,
        payload=payload,
    )


def static_findings_to_evidence(findings: list[dict], tool: str) -> list[Evidence]:
    """Convert the dicts returned by ``run_static_check`` into Evidence.

    Each finding dict is ``{file, line, severity, message, code, tool}``.
    Findings get stable ordinal refs (``static:<tool>:1``, ``static:<tool>:2``,
    ...) so review agents can cite them before they are stored.
    """
    result: list[Evidence] = []
    for i, finding in enumerate(findings, start=1):
        payload = {
            "code": finding.get("code", ""),
            "message": finding.get("message", ""),
            "severity": finding.get("severity", ""),
            "tool": tool,
        }
        result.append(
            Evidence(
                id=make_ref("static_analysis", tool, str(i)),
                kind="static_analysis",
                source=tool,
                file=finding.get("file"),
                line_start=finding.get("line"),
                line_end=finding.get("line"),
                payload=payload,
            )
        )
    return result


def from_command(
    name: str,
    command: str,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    file: str | None = None,
    line_start: int | None = None,
) -> Evidence:
    """Produce kind='command' Evidence for verifier stages.

    ``source`` is the command name; full argv + output live in ``payload``.
    """
    payload = {
        "name": name,
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }
    return Evidence(
        id="",
        kind="command",
        source=name,
        file=file,
        line_start=line_start,
        line_end=line_start,
        payload=payload,
    )


# ── External analyzer dispatch (R2.4 criterion 4 — DESIGN_v2.md §6.1, §16) ──
#
# The engine/analyzers package turns external security scanners (semgrep,
# ast-grep, trivy, gitleaks) into evidence producers. These two helpers keep
# the package wiring in one place; the per-tool parsers/runners live in
# engine/analyzers/*.py and are imported lazily (no import cycle).

_ANALYZER_RUNNERS = {
    "semgrep": ("engine.analyzers.semgrep", "run_semgrep", "parse_semgrep_json"),
    "ast-grep": ("engine.analyzers.astgrep", "run_astgrep", "parse_astgrep_json"),
    "astgrep": ("engine.analyzers.astgrep", "run_astgrep", "parse_astgrep_json"),
    "trivy": ("engine.analyzers.trivy", "run_trivy", "parse_trivy_json"),
    "gitleaks": ("engine.analyzers.gitleaks", "run_gitleaks", "parse_gitleaks_json"),
}


def run_analyzers(
    workdir: str, tools: tuple[str, ...] = ("semgrep", "ast-grep", "trivy", "gitleaks")
) -> list[Evidence]:
    """Run external analyzers and collect every finding as Evidence.

    Each tool's ``run_*`` function skips gracefully (logged note, no evidence)
    when its binary is not installed. Unknown tool names are logged and
    skipped; ``astgrep`` is accepted as an alias for ``ast-grep``.
    """
    result: list[Evidence] = []
    for tool in tools:
        entry = _ANALYZER_RUNNERS.get(tool)
        if entry is None:
            logger.warning("Unknown analyzer tool %r — skipping", tool)
            continue
        module_name, run_name, _ = entry
        module = importlib.import_module(module_name)
        result.extend(getattr(module, run_name)(workdir))
    return result


def parse_analyzer_output(tool: str, data: dict | list) -> list[Evidence]:
    """Parse a tool's raw JSON output (as decoded by ``json.loads``) into Evidence.

    Dispatches by tool name to the matching ``parse_*_json`` function; raises
    ``ValueError`` for unknown tools. ``astgrep`` is accepted as an alias for
    ``ast-grep``.
    """
    entry = _ANALYZER_RUNNERS.get(tool)
    if entry is None:
        raise ValueError(f"unknown analyzer tool: {tool!r}")
    module_name, _, parse_name = entry
    module = importlib.import_module(module_name)
    return getattr(module, parse_name)(data)


__all__ = [
    "from_static_diag",
    "static_findings_to_evidence",
    "from_command",
    "run_analyzers",
    "parse_analyzer_output",
]
