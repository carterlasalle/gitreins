"""R2.4 Evidence producers — wrap existing parsers as evidence (DESIGN_v2.md §6).

The normalized parsers in ``engine/static_analysis.py`` stay as-is; these
producers convert their output (``StaticDiag`` objects / the dicts returned by
``run_static_check``) into Evidence. Pipeline shape per the design:
"Semgrep warning -> Evidence E82 -> review agent considers it".

``from_command`` produces kind='command' evidence for future verifier stages.
"""

from __future__ import annotations

from engine.evidence.models import Evidence, make_ref
from engine.static_analysis import StaticDiag


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


__all__ = ["from_static_diag", "static_findings_to_evidence", "from_command"]
