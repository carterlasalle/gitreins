"""
R2.9 CommentWriter — batched review-comment generation (DESIGN_v2.md §15
call budget, §16 ``writer.py``, §17 ``publish`` step).

The last Lane-B stage: ranked + deduped findings (ranker.py / dedup.py) plus
change context go into ONE prompt, and the writer model (the
``review.models.writer`` config block — qwen/qwen3.7-flash per DESIGN_v2.md
§3; env-default fallback, never raises) returns ALL comments in a single
:class:`CommentBatch` — one LLM call, not one per finding (§15:
"Comment writer | Qwen Flash | batched").

:class:`CommentWriter` mirrors the VerifierAgent/ReviewAgent pattern: an
AgentRunner subclass with ``MODEL_ROLE = 'writer'`` so the ModelRouter
resolves the per-role client; ``tools=[]`` — all context arrives in the
prompt, so the writer is testable with a stub LLM.

NOTE: no ``from __future__ import annotations`` here — the schema dataclasses
must carry real types so ``parse_response`` can instantiate the nested
``Comment`` objects.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable

from engine.agents import AgentRunner, Budget
from engine.agents.schemas import schema_to_prompt
from engine.review.ranker import (
    finding_confidence,
    finding_evidence,
    finding_execution_path_confirmed,
    finding_severity,
    finding_to_dict,
)
from engine.review.reviewers import SEVERITIES

__all__ = [
    "Comment",
    "CommentBatch",
    "CommentWriter",
    "serialize_findings",
]


@dataclass
class Comment:
    """One review comment targeting a file:line (DESIGN_v2.md §15 output)."""

    file: str = field(
        metadata={"description": "Path of the file the comment refers to, relative to repo root."}
    )
    severity: str = field(
        metadata={"description": "Severity of the issue: critical, high, medium, low, or info."}
    )
    title: str = field(metadata={"description": "Short headline for the comment."})
    body: str = field(
        metadata={
            "description": (
                "The comment text: what is wrong, why it matters, and a concrete fix suggestion."
            )
        }
    )
    line: int | None = field(
        default=None,
        metadata={"description": "Line number in the file, if applicable."},
    )
    finding_ids: list[str] = field(
        default_factory=list,
        metadata={
            "description": (
                "Finding ids that motivated this comment, e.g. ['F19']. May "
                "cover several deduped findings sharing one comment."
            )
        },
    )

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict (for pipeline step data)."""
        return {
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "finding_ids": list(self.finding_ids),
        }


@dataclass
class CommentBatch:
    """Structured output of one writer run: ALL comments in one batch."""

    comments: list[Comment] = field(
        default_factory=list,
        metadata={
            "description": (
                "All review comments for this change, batched into one "
                "response. Empty when the change is clean."
            )
        },
    )
    summary: str = field(
        default="",
        metadata={"description": "One-paragraph summary of the review for the developer."},
    )


def _finding_id(f: dict, index: int) -> str:
    """The finding's own id (``F19``) or a deterministic synthetic id (``F1``...)."""
    return str(f.get("finding_id") or f"F{index}")


def serialize_findings(findings: Iterable[Any]) -> str:
    """Render ranked+deduped findings as one prompt block each.

    Each block carries the finding id (or a deterministic synthetic ``F<n>``),
    file:line, severity/impact, claim, evidence refs, and — when present —
    the verifier's verdict/confidence/execution-path factors, so the writer
    can cite exact finding ids in its comments.
    """
    blocks = []
    for i, finding in enumerate(findings, 1):
        f = finding_to_dict(finding)
        fid = _finding_id(f, i)
        loc = f.get("file") or "<repo>"
        if isinstance(f.get("line"), int):
            loc = f"{loc}:{f['line']}"
        severity = finding_severity(f)
        lines = [f"{fid} | {loc} | {severity or 'unspecified'}"]
        claim = str(f.get("claim") or f.get("notes") or "")
        if claim:
            lines.append(f"  Claim: {claim}")
        refs = finding_evidence(f)
        if refs:
            lines.append("  Evidence: " + ", ".join(refs))
        factors = []
        if f.get("verdict"):
            factors.append(f"verdict={f['verdict']}")
        conf = finding_confidence(f)
        if conf:
            factors.append(f"confidence={conf:.2f}")
        if finding_execution_path_confirmed(f):
            factors.append("execution_path_confirmed")
        if factors:
            lines.append("  Verified: " + ", ".join(factors))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


#: System prompt shared by all writer runs — the batched framing.
_SYSTEM_PROMPT_TEMPLATE = """\
You are the comment writer in a code-review ensemble (GitReins 2.0, Lane B: \
publish). The ranked, verified findings below have already survived \
falsification — your job is to turn them into review comments developers can \
act on. Do NOT invent new issues and do NOT re-litigate findings: write one \
comment per distinct issue at its exact file:line, with a concrete fix \
suggestion.

Write ALL comments in ONE response — a single batch, never one comment per \
call (DESIGN_v2.md §15: batched). Cover every finding; findings that were \
deduped may share a comment, but never drop a finding from the batch. \
Reference the finding ids you are addressing.

Severity levels: {severities}.

{output_schema}
"""


class CommentWriter(AgentRunner):
    """Batched comment writer (R2.9, DESIGN_v2.md §15/§16/§17).

    ``run(findings, ...)`` drives the generic bounded loop with the
    comment-writer system prompt, ``tools=[]`` (all context in the prompt),
    the :class:`CommentBatch` output schema, and ``model_role='writer'``
    (resolved from the ``review.models.writer`` config block — DESIGN_v2.md
    §3, qwen/qwen3.7-flash; absent → env-default client, never raises).
    One writer call produces ALL comments (batched).
    """

    MODEL_ROLE = "writer"

    def run(
        self,
        findings: Any,
        *,
        changed_files: list[str] | None = None,
        diff_context: str = "",
        budget: Budget | None = None,
    ) -> CommentBatch:
        """Write ALL review comments for the ranked + deduped findings.

        Args:
            findings: The ranked and deduped findings (ranker.py / dedup.py
                output) — a list of finding dataclasses/dicts, or a
                ReviewFindings/VerifierFindings wrapper carrying ``.findings``.
            changed_files: Files changed by the diff (context).
            diff_context: Raw diff text (context).
            budget: Agent Budget; defaults to an unlimited Budget.

        Returns:
            A typed :class:`CommentBatch` — all comments in one batch.
        """
        items = self._as_finding_list(findings)
        parts = []
        if changed_files:
            files = "\n".join(f"- {f}" for f in changed_files)
            parts.append(f"Changed files:\n{files}")
        if diff_context:
            parts.append(f"Diff context:\n{diff_context}")
        serialized = serialize_findings(items)
        parts.append(
            "Ranked findings to turn into comments:\n"
            + (serialized or "(none — the change is clean)")
        )
        parts.append("Produce ALL review comments now — a single batch.")

        return super().run(
            system_prompt=_SYSTEM_PROMPT_TEMPLATE.format(
                severities=", ".join(SEVERITIES),
                output_schema=schema_to_prompt(CommentBatch),
            ),
            user_prompt="\n\n".join(parts),
            tools=[],
            output_schema=CommentBatch,
            model_role=self.MODEL_ROLE,
            budget=budget if budget is not None else Budget(),
        )

    @staticmethod
    def _as_finding_list(findings: Any) -> list:
        """Accept a list/tuple of findings or a ``.findings`` wrapper."""
        if isinstance(findings, (list, tuple)):
            return list(findings)
        items = getattr(findings, "findings", None)
        if items is not None:
            return list(items)
        raise TypeError(
            f"expected a list of findings or a findings wrapper, got {type(findings).__name__}"
        )
