"""
R2.6 ScoutAgent — cheap retrieval-plan classifier (DESIGN_v2.md §7).

The scout runs **before** the agent loop: given the changed-file list and
diff context, a cheap model produces a **retrieval plan** — NOT a review.
The deterministic EvidencePlanner (engine/review/context_builder.py) then
executes that plan against code-intel providers:

    "callers of foo → 3 callers"  (system does the retrieval, not the model)

Output schema (:class:`ScoutPlan`)::

    {
      "changed_symbols": [ { "symbol": "SessionManager.rotate_token",
                             "risk": "high" } ],
      "retrieval_requests": [ { "type": "callers", "query": "SessionManager.rotate_token" },
                              { "type": "references", "query": "refresh_token" } ],
      "review_lenses": [ "state/concurrency", "authorization" ]
    }

``model_role`` is hardcoded to ``"scout"`` so the runner resolves the cheap
model from the ``review.models.scout`` config block via
``ModelRouter.for_role("scout")``; when the block is absent the router falls
back to an env-default client (it never raises).

NOTE: no ``from __future__ import annotations`` here — the schema dataclasses
must carry real types so ``parse_response`` can instantiate the nested
``ChangedSymbol`` / ``RetrievalRequest`` objects.
"""

from dataclasses import dataclass, field

from engine.agents import AgentRunner, Budget
from engine.agents.schemas import schema_to_prompt

__all__ = ["ChangedSymbol", "RetrievalRequest", "ScoutPlan", "ScoutAgent"]


@dataclass
class ChangedSymbol:
    """A symbol the diff touches, with the scout's risk estimate."""

    symbol: str = field(
        metadata={"description": "Fully-qualified symbol name, e.g. SessionManager.rotate_token."}
    )
    risk: str = field(
        metadata={"description": "Risk of the change: low, medium, or high."}
    )


@dataclass
class RetrievalRequest:
    """One code-intel retrieval for the evidence planner to execute."""

    type: str = field(
        metadata={
            "description": (
                "Retrieval type: callers, references, definition, "
                "implementations, callees, or symbols."
            )
        }
    )
    query: str = field(
        metadata={"description": "Symbol name or search pattern to retrieve."}
    )


@dataclass
class ScoutPlan:
    """The scout's structured output: what changed, what to retrieve, what to review."""

    changed_symbols: list[ChangedSymbol] = field(
        metadata={"description": "Symbols changed by this diff, each with a risk estimate."}
    )
    retrieval_requests: list[RetrievalRequest] = field(
        metadata={
            "description": (
                "Code-intel retrievals the evidence planner should execute "
                "to let reviewers verify the change is safe."
            )
        }
    )
    review_lenses: list[str] = field(
        metadata={
            "description": (
                "Review lenses to apply, e.g. state/concurrency, "
                "authorization, error handling, resource lifecycle."
            )
        }
    )


#: The scout's system prompt — a cheap classifier, not a reviewer.
_SYSTEM_PROMPT_TEMPLATE = """\
You are the retrieval scout for a code review system. Given the changed files \
and diff context, produce a RETRIEVAL PLAN — not a review. You never judge \
whether code is correct; you decide what a reviewer needs to look at.

For each symbol the diff touches, estimate the risk of the change:
- high: exported/central API, shared state, auth/security-sensitive code
- medium: called from several places, behavior changes
- low: local or leaf change

Then request code-intel retrievals that let a reviewer verify the change is \
safe: callers of changed functions, references to renamed symbols, \
definitions of new symbols, implementations of changed interfaces, callees \
of rewritten functions, and symbol lookups.

Finally choose the review lenses most relevant to this diff (e.g. \
state/concurrency, authorization, error handling, resource lifecycle, data \
validation, performance, backward compatibility).

{output_schema}
"""


class ScoutAgent(AgentRunner):
    """Cheap classifier: diff → retrieval plan (DESIGN_v2.md §7).

    ``run(changed_files, diff_context)`` drives the generic bounded agent
    loop (AgentRunner) with ``model_role="scout"`` and the :class:`ScoutPlan`
    output schema, returning a typed plan. No tools are needed — planning is
    pure classification.
    """

    MODEL_ROLE = "scout"

    def run(
        self,
        changed_files: list[str],
        diff_context: str = "",
        *,
        budget: Budget | None = None,
    ) -> ScoutPlan:
        """Classify ``changed_files`` (+ optional ``diff_context``) into a ScoutPlan."""
        files = "\n".join(f"- {f}" for f in changed_files) or "(no changed files listed)"
        parts = [f"Changed files:\n{files}"]
        if diff_context:
            parts.append(f"Diff context:\n{diff_context}")
        parts.append("Produce the retrieval plan now.")

        return super().run(
            system_prompt=_SYSTEM_PROMPT_TEMPLATE.format(
                output_schema=schema_to_prompt(ScoutPlan)
            ),
            user_prompt="\n\n".join(parts),
            tools=[],
            output_schema=ScoutPlan,
            model_role=self.MODEL_ROLE,
            budget=budget if budget is not None else Budget(),
        )
