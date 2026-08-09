"""
R2.7 Review agents — Lane B: defect discovery (DESIGN_v2.md §8).

Three AgentRunner subclasses, one per review lane, all fed the **same
evidence store** with a **distinct objective/system prompt**:

    RuntimeReviewer   — crashes, races, resource leaks, control flow
    ContractReviewer  — API/interface breakage, invariants, validation
    SecurityReviewer  — authz bypass, injection, secrets, crypto

Each ``run()`` serializes the evidence store contents into its user prompt
(evidence id + kind + location + payload — NOT a tool), so every finding a
model produces can cite ``E17`` / ``static:semgrep:17``. The output schema is
:class:`ReviewFindings`, a list of :class:`ReviewFinding` with
file/line/claim/severity/evidence.

``model_role`` is hardcoded per agent (``runtime_reviewer`` /
``contract_reviewer`` / ``security_reviewer``) so the router resolves the
per-role model from the ``review.models.<role>`` config block (DESIGN_v2.md
§3); a missing block falls back to env defaults (never raises).

NOTE: no ``from __future__ import annotations`` here — the schema dataclasses
must carry real types so ``parse_response`` can instantiate the nested
``ReviewFinding`` objects.
"""

import json
from dataclasses import dataclass, field

from engine.agents import AgentRunner, Budget
from engine.agents.schemas import schema_to_prompt
from engine.evidence.store import EvidenceStore

__all__ = [
    "ReviewFinding",
    "ReviewFindings",
    "ReviewAgent",
    "RuntimeReviewer",
    "ContractReviewer",
    "SecurityReviewer",
    "ROLE_TO_REVIEWER",
    "DEFAULT_REVIEWER_ROLES",
    "serialize_evidence",
]

#: Severity levels a reviewer may assign (DESIGN_v2.md §12 style, no scores).
SEVERITIES = ("critical", "high", "medium", "low", "info")


@dataclass
class ReviewFinding:
    """A single defect claim produced by a review agent.

    Every claim MUST reference evidence by store id (``E17``) or tool ref
    (``static:semgrep:17``) — never "assertions from nowhere" (§6).
    """

    claim: str = field(
        metadata={"description": "What is wrong: the defect and why it matters."}
    )
    severity: str = field(
        metadata={
            "description": (
                "Severity of the defect: critical, high, medium, low, or info."
            )
        }
    )
    file: str = field(
        default="",
        metadata={
            "description": "Path of the file the finding refers to, relative to repo root."
        },
    )
    line: int | None = field(
        default=None,
        metadata={"description": "Line number in the file, if applicable."},
    )
    evidence: list[str] = field(
        default_factory=list,
        metadata={
            "description": (
                "Evidence store refs backing this claim, e.g. E17 or static:semgrep:17."
            )
        },
    )
    category: str = field(
        default="",
        metadata={
            "description": (
                "Finding category within this review lane, e.g. null-deref, race, "
                "authz-missing."
            )
        },
    )
    suggestion: str = field(
        default="",
        metadata={"description": "Optional concrete fix suggestion."},
    )

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict (for pipeline step data)."""
        return {
            "file": self.file,
            "line": self.line,
            "claim": self.claim,
            "severity": self.severity,
            "evidence": list(self.evidence),
            "category": self.category,
            "suggestion": self.suggestion,
        }


@dataclass
class ReviewFindings:
    """Structured output of one reviewer run: findings + a summary."""

    findings: list[ReviewFinding] = field(
        default_factory=list,
        metadata={
            "description": (
                "The defects found, each with file/line/claim/severity and "
                "evidence refs. Empty when the change is clean in this lane."
            )
        },
    )
    summary: str = field(
        default="",
        metadata={
            "description": (
                "One-paragraph summary of this lane's review verdict."
            )
        },
    )


#: System prompt shared by all review agents — role-specific parts injected.
_SYSTEM_PROMPT_TEMPLATE = """\
You are the {role_name} agent in a code-review ensemble (GitReins 2.0, Lane B: \
defect discovery). Your job is to find DEFECTS in the changed code — not to \
praise it, and not to fix it.

Objective:
{objective}

You review the changed files and diff context given below, using the evidence \
store contents (diff hunks, code-intel retrieval results, static-analysis \
findings) attached to the prompt. Every claim you make MUST reference \
evidence by its store id (E1, E17) or tool ref (static:semgrep:17) — never \
make assertions from nowhere. If you have no evidence for a claim, either \
collect it or downgrade the claim.

Severity levels: {severities}.

{output_schema}
"""


def serialize_evidence(store: EvidenceStore) -> str:
    """Render an EvidenceStore as one prompt-friendly line per item.

    Format: ``E1 [diff] auth/session.py:41-45 source=git {payload-json}`` —
    the model can cite the leading id directly. Payloads are capped at 500
    chars so a large store cannot blow the prompt budget.
    """
    lines = []
    for ev in store.all():
        loc = f"{ev.file or '<repo>'}:{ev.line_start or '?'}"
        if ev.line_end is not None and ev.line_end != ev.line_start:
            loc += f"-{ev.line_end}"
        try:
            payload = json.dumps(ev.payload, sort_keys=True)
        except (TypeError, ValueError):
            payload = str(ev.payload)
        if len(payload) > 500:
            payload = payload[:500] + "…"
        lines.append(f"{ev.id} [{ev.kind}] {loc} source={ev.source} {payload}")
    return "\n".join(lines)


class ReviewAgent(AgentRunner):
    """Base class for Lane-B review agents (R2.7).

    Subclasses set ``MODEL_ROLE`` (the ``review.models.<role>`` config key)
    and ``OBJECTIVE`` (their distinct review lane). ``run()`` drives the
    generic bounded agent loop with ``tools=[]`` — all context arrives in the
    user prompt as serialized evidence, so reviewers are testable with a stub
    LLM and never touch the sandbox.
    """

    MODEL_ROLE = "reviewer"
    OBJECTIVE = ""
    _ROLE_NAME = "review agent"

    def run(
        self,
        evidence_store: EvidenceStore | None = None,
        *,
        changed_files: list[str] | None = None,
        diff_context: str = "",
        lenses: list[str] | None = None,
        intent_context: str | list[dict] | None = None,
        budget: Budget | None = None,
    ) -> ReviewFindings:
        """Review the evidence store for defects in this lane.

        Args:
            evidence_store: The shared evidence store (R2.4); its contents are
                serialized into the user prompt. None → an empty store.
            changed_files: Files changed by the diff (for context).
            diff_context: Raw diff text (for context).
            lenses: Review lenses the scout selected (DESIGN_v2.md §7), if any.
            intent_context: Task intent context (DESIGN_v2.md §10) — either a
                pre-rendered 'Task intent (developer criteria)' text block or
                a list of ``{id, title, criteria, status}`` dicts (as returned
                by ``TaskManager.intent_context``). Rendered into the user
                prompt as its own section BEFORE the evidence store, so the
                reviewer sees what the developer tried to do alongside what
                they accidentally broke. None → no intent section.
            budget: Agent Budget; defaults to an unlimited Budget.

        Returns:
            A typed :class:`ReviewFindings` with the lane's findings.
        """
        store = evidence_store if evidence_store is not None else EvidenceStore()
        parts = []
        if changed_files:
            files = "\n".join(f"- {f}" for f in changed_files)
            parts.append(f"Changed files:\n{files}")
        if diff_context:
            parts.append(f"Diff context:\n{diff_context}")
        if lenses:
            lens_lines = "\n".join(f"- {lens}" for lens in lenses)
            parts.append(f"Review lenses selected by the scout:\n{lens_lines}")
        if intent_context:
            if isinstance(intent_context, str):
                intent_block = intent_context
            else:
                from engine.task_manager import intent_to_prompt

                intent_block = intent_to_prompt(intent_context)
            if intent_block:
                parts.append(intent_block)
        evidence_text = serialize_evidence(store)
        parts.append(
            "Evidence store:\n" + (evidence_text or "(empty — review the diff directly)")
        )
        parts.append("Produce your structured findings now.")

        return super().run(
            system_prompt=_SYSTEM_PROMPT_TEMPLATE.format(
                role_name=self._ROLE_NAME,
                objective=self.OBJECTIVE,
                severities=", ".join(SEVERITIES),
                output_schema=schema_to_prompt(ReviewFindings),
            ),
            user_prompt="\n\n".join(parts),
            tools=[],
            output_schema=ReviewFindings,
            model_role=self.MODEL_ROLE,
            budget=budget if budget is not None else Budget(),
        )


class RuntimeReviewer(ReviewAgent):
    """Finds runtime defects: crashes, races, leaks, control flow."""

    MODEL_ROLE = "runtime_reviewer"
    _ROLE_NAME = "runtime reviewer"
    OBJECTIVE = (
        "Find runtime defects in the changed code: crashes and exceptions "
        "(null/None dereferences, unhandled errors, wrong exception types), "
        "concurrency and state bugs (races, shared mutable state, missing "
        "synchronization), resource lifecycle leaks (files, locks, "
        "connections, timers), and incorrect control flow (off-by-one, wrong "
        "conditions, swallowed errors)."
    )


class ContractReviewer(ReviewAgent):
    """Finds contract violations: API breakage, invariants, validation."""

    MODEL_ROLE = "contract_reviewer"
    _ROLE_NAME = "contract reviewer"
    OBJECTIVE = (
        "Find contract violations introduced by the change: breaking API or "
        "interface signature changes, callers left incompatible with new "
        "callee behavior, violated invariants and postconditions, missing or "
        "incorrect input validation, and backward-compatibility breaks "
        "(renamed symbols, changed return types, removed parameters)."
    )


class SecurityReviewer(ReviewAgent):
    """Finds security defects: authz bypass, injection, secrets, crypto."""

    MODEL_ROLE = "security_reviewer"
    _ROLE_NAME = "security reviewer"
    OBJECTIVE = (
        "Find security defects in the changed code: authentication and "
        "authorization bypasses, injection (SQL, command, XSS, path "
        "traversal), unsafe handling of secrets or credentials, insecure "
        "crypto (weak algorithms, hardcoded keys), untrusted input reaching "
        "dangerous sinks, and privilege or access-control regressions."
    )


#: Pipeline role name → reviewer class. DESIGN_v2.md §8 uses the short names
#: (runtime / contracts / security_edges); §17 and the review.models config
#: block use the *_reviewer spellings — both are accepted.
ROLE_TO_REVIEWER: dict[str, type[ReviewAgent]] = {
    "runtime": RuntimeReviewer,
    "contracts": ContractReviewer,
    "security_edges": SecurityReviewer,
    "runtime_reviewer": RuntimeReviewer,
    "contract_reviewer": ContractReviewer,
    "security_reviewer": SecurityReviewer,
}

#: The three reviewers the orchestrator runs in parallel by default.
DEFAULT_REVIEWER_ROLES: tuple[str, ...] = (
    "runtime_reviewer",
    "contract_reviewer",
    "security_reviewer",
)
