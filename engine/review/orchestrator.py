"""
R2.7 ReviewOrchestrator — the Lane-B review DAG driver (DESIGN_v2.md §8).

Runs the review DAG: scout → evidence planner → parallel review agents →
merged findings list. Two entry points:

    run(changed_files, diff)          — full DAG (own entry: diff evidence →
                                         scout → optional code-intel retrieval
                                         → reviewers)
    run_from_plan(plan, store, ...)   — pipeline output entry: consumes an
                                         existing ScoutPlan + EvidenceStore
                                         (produced by ScoutAgent/EvidencePlanner
                                         upstream) and runs only the parallel
                                         reviewers.

Reviewers run concurrently via ThreadPoolExecutor. A failure in one reviewer
is captured as a per-agent error (ReviewerResult.ok=False) and never kills
the DAG — the other lanes' findings are still collected.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from engine.agents import Budget
from engine.evidence.models import Evidence
from engine.evidence.store import EvidenceStore
from engine.router import ModelRouter
from engine.review.context_builder import EvidencePlanner
from engine.review.history import ReviewRunArchiver
from engine.review.scout import ScoutAgent, ScoutPlan
from engine.review.reviewers import (
    DEFAULT_REVIEWER_ROLES,
    ROLE_TO_REVIEWER,
    ReviewAgent,
    ReviewFinding,
    ReviewFindings,
)

logger = logging.getLogger("gitreins.review.orchestrator")

__all__ = [
    "ReviewerResult",
    "ReviewResult",
    "ReviewOrchestrator",
    "DEFAULT_REVIEWER_ROLES",
]

#: Evidence source tag for diff evidence appended by the own-run entry.
DIFF_EVIDENCE_SOURCE = "diff"


@dataclass
class ReviewerResult:
    """Outcome of one review agent run (per lane)."""

    role: str
    findings: list[ReviewFinding] = field(default_factory=list)
    ok: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "ok": self.ok,
            "error": self.error,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class ReviewResult:
    """Merged output of the parallel review stage."""

    findings: list[ReviewFinding] = field(default_factory=list)
    per_agent: dict[str, ReviewerResult] = field(default_factory=dict)
    evidence_store: EvidenceStore | None = None

    @property
    def errors(self) -> dict[str, str]:
        """Role → error message for every failed reviewer."""
        return {r: res.error for r, res in self.per_agent.items() if not res.ok}

    @property
    def all_ok(self) -> bool:
        return all(res.ok for res in self.per_agent.values())

    def to_dict(self) -> dict:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "per_agent": {r: res.to_dict() for r, res in self.per_agent.items()},
            "evidence_count": len(self.evidence_store.all()) if self.evidence_store else 0,
            "errors": self.errors,
            "all_ok": self.all_ok,
        }


class ReviewOrchestrator:
    """Run the review DAG and merge the parallel lanes' findings.

    Args:
        router: ModelRouter (or duck-typed stand-in) resolving a client per
            role; None → built from ``config`` (or env defaults).
        workdir: Repo root passed to the agents.
        config: ``.gitreins/config.yaml``-shaped dict (model routing).
        provider: Optional CodeIntelProvider for the evidence planner — when
            set, the own-run entry executes the scout's retrieval requests;
            when None, retrieval is skipped and reviewers see only the
            evidence already in the store.
        agents: Optional pre-built {role: ReviewAgent} instances (test
            injection); otherwise reviewers are constructed per role.
        default_limit: Per-request retrieval cap for the evidence planner.
        scout_budget / reviewer_budget: Optional per-phase Budgets.
        archiver: Optional ReviewRunArchiver for §13 rich review history;
            None → a default archiver is created with the repo root as its
            base dir, so every completed run is persisted under
            ``review_runs/<commit-sha>/`` (archiving never raises).
    """

    def __init__(
        self,
        router: Any | None = None,
        *,
        workdir: str = ".",
        config: dict | None = None,
        provider: Any | None = None,
        agents: dict[str, ReviewAgent] | None = None,
        default_limit: int = 3,
        scout_budget: Budget | None = None,
        reviewer_budget: Budget | None = None,
        archiver: ReviewRunArchiver | None = None,
    ):
        self.workdir = os.path.abspath(workdir)
        self.config = config or {}
        self.router = router if router is not None else ModelRouter(self.config)
        self.provider = provider
        self.default_limit = default_limit
        self._agents: dict[str, ReviewAgent] = dict(agents or {})
        self._scout_budget = scout_budget
        self._reviewer_budget = reviewer_budget
        self._archiver = (
            archiver if archiver is not None else ReviewRunArchiver(base_dir=self.workdir)
        )

    # ── Entry points ──────────────────────────────────────────────

    def run(
        self,
        changed_files: list[str],
        diff_context: str = "",
        *,
        evidence_store: EvidenceStore | None = None,
        roles: list[str] | None = None,
        intent_context: str | list[dict] | None = None,
    ) -> ReviewResult:
        """Full DAG: diff evidence → scout → retrieval → parallel reviewers.

        Appends one ``diff`` Evidence per changed file, classifies the change
        with ScoutAgent, executes the resulting retrieval plan through the
        EvidencePlanner when a ``provider`` is configured, then runs the
        reviewers in parallel over the shared store. ``intent_context`` (a
        rendered 'Task intent (developer criteria)' block or a list of
        ``{id, title, criteria, status}`` dicts, DESIGN_v2.md §10) is threaded
        to every reviewer.
        """
        store = evidence_store if evidence_store is not None else EvidenceStore()
        self._append_diff_evidence(store, changed_files, diff_context)

        scout = ScoutAgent(router=self.router, workdir=self.workdir)
        plan = scout.run(
            changed_files,
            diff_context,
            budget=self._scout_budget if self._scout_budget is not None else Budget(),
        )

        if self.provider is not None:
            planner = EvidencePlanner(self.provider, store, default_limit=self.default_limit)
            planner.execute(plan)

        return self.run_from_plan(
            plan,
            store,
            changed_files=changed_files,
            diff_context=diff_context,
            roles=roles,
            intent_context=intent_context,
        )

    def run_from_plan(
        self,
        plan: ScoutPlan,
        evidence_store: EvidenceStore | None = None,
        *,
        changed_files: list[str] | None = None,
        diff_context: str = "",
        roles: list[str] | None = None,
        intent_context: str | list[dict] | None = None,
    ) -> ReviewResult:
        """Run the parallel reviewers over an existing pipeline output.

        ``plan`` and ``evidence_store`` are the ScoutAgent/EvidencePlanner
        pipeline output (DESIGN_v2.md §7): the store already holds the
        evidence packet, and the plan's ``review_lenses`` are passed to each
        reviewer as context. ``intent_context`` (rendered block or
        ``{id, title, criteria, status}`` dicts, §10) is threaded to every
        reviewer. A failing lane degrades to a per-agent error — the DAG
        never raises.
        """
        store = evidence_store if evidence_store is not None else EvidenceStore()
        role_list = list(roles) if roles else list(DEFAULT_REVIEWER_ROLES)
        lenses = list(getattr(plan, "review_lenses", []) or [])

        per_agent: dict[str, ReviewerResult] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(role_list)) as executor:
            futures = {
                executor.submit(
                    self._run_one,
                    role,
                    store,
                    changed_files,
                    diff_context,
                    lenses,
                    intent_context,
                ): role
                for role in role_list
            }
            for future in concurrent.futures.as_completed(futures):
                role = futures[future]
                try:
                    per_agent[role] = future.result()
                except Exception as exc:  # noqa: BLE001 — net over _run_one
                    logger.exception("Review lane %r crashed", role)
                    per_agent[role] = ReviewerResult(role=role, ok=False, error=str(exc))

        # Deterministic ordering: report lanes in request order, not completion order.
        per_agent = {role: per_agent[role] for role in role_list if role in per_agent}

        findings: list[ReviewFinding] = []
        for res in per_agent.values():
            findings.extend(res.findings)
        result = ReviewResult(findings=findings, per_agent=per_agent, evidence_store=store)
        self._stamp_generated_by(result)
        self._archive_run(plan, store, changed_files, diff_context, result, intent_context)
        return result

    # ── Internals ─────────────────────────────────────────────────

    def _stamp_generated_by(self, result: ReviewResult) -> None:
        """Attach §13 ``generated_by`` {role, model} provenance to every finding.

        The lane that produced a finding is known at merge time (per_agent),
        so provenance is stamped here. The model name is resolved best-effort
        from a real ModelRouter; duck-typed test routers are never queried
        (they record for_role calls) and their stub clients carry no model
        name, so provenance degrades to an empty model — it never breaks the
        DAG.
        """
        for role, res in result.per_agent.items():
            model = self._model_for_role(role)
            for finding in res.findings:
                if isinstance(finding, ReviewFinding):
                    finding.generated_by = {"role": role, "model": model}

    def _model_for_role(self, role: str) -> str:
        """Best-effort model name for provenance; '' when unavailable."""
        if not isinstance(self.router, ModelRouter):
            return ""
        try:
            client = self.router.for_role(role)
            return str(getattr(client, "model", "") or "")
        except Exception:  # noqa: BLE001 — provenance must never break the DAG
            return ""

    def _archive_run(
        self,
        plan: ScoutPlan,
        store: EvidenceStore,
        changed_files: list[str] | None,
        diff_context: str,
        result: ReviewResult,
        intent_context: str | list[dict] | None,
    ) -> None:
        """Persist the completed run under ``review_runs/<sha>/`` (§13).

        Archives every stage that has data: change, scout, candidates,
        final findings (with provenance), requirements, usage, and the
        manifest. Static-evidence is archived when the store carries
        static-analysis/LSP evidence; verification is not part of the
        orchestrator's DAG (it is wired upstream, R2.16). Archiving is
        best-effort — a failure is logged and never breaks the review.
        """
        try:
            self._archiver.archive_all(
                change={
                    "changed_files": list(changed_files or []),
                    "diff_context": diff_context or "",
                },
                static_evidence=self._static_evidence(store),
                scout=plan,
                candidates=result.findings,
                final_findings=result.findings,
                requirements=self._requirements(store, intent_context),
                usage={
                    "roles": list(result.per_agent.keys()),
                    "findings_per_agent": {
                        r: len(res.findings) for r, res in result.per_agent.items()
                    },
                    "total_findings": len(result.findings),
                    "evidence_count": len(store.all()),
                    "errors": result.errors,
                    "all_ok": result.all_ok,
                },
                manifest={"workdir": self.workdir},
            )
        except Exception as exc:  # noqa: BLE001 — archive is best-effort; never break the DAG
            logger.warning("Review-run archiving failed: %s", exc)

    def _static_evidence(self, store: EvidenceStore) -> list[Evidence] | None:
        """Static-analysis/LSP evidence for static-evidence.json; None when empty."""
        items = list(store.query(kind="static_analysis")) + list(store.query(kind="lsp"))
        return items or None

    def _requirements(
        self,
        store: EvidenceStore,
        intent_context: str | list[dict] | None,
    ) -> list[dict] | None:
        """Requirement evidence + structured intent for requirements.json."""
        reqs: list[dict] = [e.to_dict() for e in store.query(kind="requirement")]
        if isinstance(intent_context, list):
            reqs.extend(dict(r) for r in intent_context if isinstance(r, dict))
        return reqs or None

    def _run_one(
        self,
        role: str,
        store: EvidenceStore,
        changed_files: list[str] | None,
        diff_context: str,
        lenses: list[str],
        intent_context: str | list[dict] | None = None,
    ) -> ReviewerResult:
        """Run one reviewer lane; never raises (errors become the result)."""
        try:
            agent = self._agent_for(role)
            findings = agent.run(
                store,
                changed_files=changed_files,
                diff_context=diff_context,
                lenses=lenses,
                intent_context=intent_context,
                budget=self._reviewer_budget if self._reviewer_budget is not None else Budget(),
            )
            items = findings.findings if isinstance(findings, ReviewFindings) else []
            return ReviewerResult(role=role, findings=list(items))
        except Exception as exc:  # noqa: BLE001 — lane boundary: degrade, don't crash
            logger.warning("Review agent %r failed: %s", role, exc)
            return ReviewerResult(role=role, ok=False, error=str(exc))

    def _agent_for(self, role: str) -> ReviewAgent:
        """Return the reviewer for ``role`` (injected instance or fresh)."""
        if role in self._agents:
            return self._agents[role]
        cls = ROLE_TO_REVIEWER.get(role)
        if cls is None:
            raise ValueError(f"unknown review role: {role!r}")
        return cls(router=self.router, workdir=self.workdir)  # type: ignore[arg-type]

    def _append_diff_evidence(
        self, store: EvidenceStore, changed_files: list[str], diff_context: str
    ) -> list[Evidence]:
        """Append one ``diff`` Evidence per changed file (own-run entry)."""
        appended: list[Evidence] = []
        for file in changed_files or []:
            appended.append(
                store.append(
                    Evidence(
                        id="",
                        kind="diff",
                        source=DIFF_EVIDENCE_SOURCE,
                        file=file,
                        line_start=None,
                        line_end=None,
                        payload={"changed": True, "diff_context": diff_context[:2000]},
                    )
                )
            )
        return appended
