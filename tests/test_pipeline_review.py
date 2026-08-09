"""
R2.7 tests for the pipeline ``review_agent`` stage type (engine/pipeline.py).

Covers: parallel review_agent stages with the three pipeline roles
(runtime / contracts / security_edges) running per-role reviewers and
reporting findings, per-role model routing through the pipeline router,
findings plumbing onto the task dict (later stages consume them), sequential
single-step review_agent stages, missing/unknown role errors, reviewer
failures captured per step without killing the parallel DAG, evidence from
either ``task["evidence_store"]`` (instance) or ``task["evidence"]`` (dicts),
and an empty evidence store degrading gracefully.

Hermetic: a RoleMapRouter returns a distinct StubLLM per role — no real
model or network.
"""

from engine.evidence import Evidence, EvidenceStore
from engine.llm import LLMResponse
from engine.pipeline import Pipeline
from engine.review import DEFAULT_REVIEWER_ROLES


def findings_json(claim):
    return (
        '{"findings": [{"claim": "' + claim + '", "severity": "medium",'
        ' "file": "a.py", "line": 5, "evidence": ["E1"],'
        ' "category": "defect"}], "summary": ""}'
    )


class StubLLM:
    """Deterministic fake LLMClient: plays back responses, records calls."""

    def __init__(self, responses, raises=None):
        self.responses = list(responses)
        self.raises = raises
        self.calls = 0
        self.messages_seen = []

    def chat(self, messages, tools=None, max_tokens=16384, temperature=0.1):
        if self.raises is not None:
            raise self.raises
        self.calls += 1
        self.messages_seen.append(list(messages))
        resp = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return resp() if callable(resp) else resp


class RoleMapRouter:
    """Returns a distinct client per role — deterministic parallel runs."""

    def __init__(self, clients):
        self.clients = dict(clients)
        self.roles = []

    def for_role(self, role):
        self.roles.append(role)
        return self.clients[role]


#: Pipeline step id (short name) → model_role (config block name).
PIPELINE_ROLES = [
    ("runtime", "runtime_reviewer"),
    ("contracts", "contract_reviewer"),
    ("security", "security_reviewer"),
]

CLAIMS = {
    "runtime_reviewer": "race on shared cache",
    "contract_reviewer": "changed return type breaks callers",
    "security_reviewer": "missing authz check",
}


def review_router(raises_role=None):
    """Per-role stubs for the three pipeline roles (one may raise)."""
    clients = {}
    for role in DEFAULT_REVIEWER_ROLES:
        raise_ = RuntimeError("LLM transport down") if role == raises_role else None
        clients[role] = StubLLM(
            [LLMResponse(content=findings_json(CLAIMS[role]))], raises=raise_
        )
    return RoleMapRouter(clients)


def review_config(parallel=True, extra_steps=None):
    """Pipeline config with a reviewers stage (parallel or single-step)."""
    steps = [
        {"id": "runtime", "type": "review_agent", "role": "runtime"},
        {"id": "contracts", "type": "review_agent", "role": "contracts"},
        {"id": "security", "type": "review_agent", "role": "security_edges"},
    ]
    if extra_steps:
        steps.extend(extra_steps)
    stage = {"id": "reviewers", "steps": steps}
    if parallel:
        stage["parallel"] = True
    return {"pipeline": {"stages": [stage]}}


def make_task(store=None):
    task = {
        "id": "review-task",
        "title": "Review the change",
        "criteria": [],
        "changed_files": ["auth/session.py"],
        "diff": "rotated refresh tokens",
        "review_lenses": ["state/concurrency"],
    }
    if store is not None:
        task["evidence_store"] = store
    return task


# ── (a) Parallel review_agent stage ──────────────────────────────────────


class TestParallelReviewAgentStage:
    def test_all_roles_run_and_report_findings(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        result = p.run(task, trigger="pre-eval")

        stage = result["stages"]["reviewers"]
        assert stage["passed"] is True
        assert len(stage["steps"]) == 3
        by_id = {s["id"]: s for s in stage["steps"]}
        for short, model_role in PIPELINE_ROLES:
            step = by_id[short]
            assert step["type"] == "review_agent"
            assert step["passed"] is True
            assert step["data"]["role"] == model_role
            assert step["data"]["count"] == 1
            assert step["data"]["findings"][0]["claim"] == CLAIMS[model_role]
            assert step["data"]["findings"][0]["severity"] == "medium"

    def test_per_role_model_routing_through_router(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        p.run(make_task(), trigger="pre-eval")
        assert set(router.roles) == set(DEFAULT_REVIEWER_ROLES)

    def test_findings_plumbed_onto_task(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        p.run(task, trigger="pre-eval")
        # Later stages consume task["findings"] — one dict per finding.
        assert len(task["findings"]) == 3
        claims = {f["claim"] for f in task["findings"]}
        assert claims == set(CLAIMS.values())
        for f in task["findings"]:
            assert set(f) >= {"file", "line", "claim", "severity", "evidence"}

    def test_evidence_store_passed_via_task(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        store = EvidenceStore()
        store.append(
            Evidence(
                id="",
                kind="diff",
                source="diff",
                file="auth/session.py",
                line_start=41,
                line_end=45,
                payload={"changed": True},
            )
        )
        p.run(make_task(store=store), trigger="pre-eval")
        # Every reviewer saw the store contents serialized in its prompt.
        for role in DEFAULT_REVIEWER_ROLES:
            user_prompt = router.clients[role].messages_seen[0][1]["content"]
            assert "E1" in user_prompt
            assert "auth/session.py:41-45" in user_prompt
            assert "rotated refresh tokens" in user_prompt  # diff context
            assert "state/concurrency" in user_prompt  # scout lens

    def test_evidence_from_task_dict_list(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        task.pop("evidence_store", None)
        task["evidence"] = [
            {
                "id": "",
                "kind": "diff",
                "source": "diff",
                "file": "auth/session.py",
                "line_start": 41,
                "line_end": 45,
                "payload": {"changed": True},
            }
        ]
        p.run(task, trigger="pre-eval")
        user_prompt = router.clients["runtime_reviewer"].messages_seen[0][1]["content"]
        assert "E1" in user_prompt  # rebuilt from the dict list
        assert "[diff]" in user_prompt

    def test_empty_evidence_store_ok(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()  # no evidence keys at all
        result = p.run(task, trigger="pre-eval")
        assert result["stages"]["reviewers"]["passed"] is True
        user_prompt = router.clients["runtime_reviewer"].messages_seen[0][1]["content"]
        assert "(empty" in user_prompt  # graceful no-evidence note


# ── (b) Errors and resilience ────────────────────────────────────────────


class TestReviewAgentErrors:
    def test_unknown_role_step_fails(self, tmp_workdir):
        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "reviewers",
                        "parallel": True,
                        "steps": [
                            {"id": "x", "type": "review_agent", "role": "bogus"}
                        ],
                    }
                ]
            }
        }
        p = Pipeline(config, tmp_workdir, router=review_router())
        result = p.run(make_task(), trigger="pre-eval")
        step = result["stages"]["reviewers"]["steps"][0]
        assert step["passed"] is False
        assert "Unknown review_agent role" in step["error"]

    def test_missing_role_step_fails(self, tmp_workdir):
        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "reviewers",
                        "parallel": True,
                        "steps": [{"id": "x", "type": "review_agent"}],
                    }
                ]
            }
        }
        p = Pipeline(config, tmp_workdir, router=review_router())
        result = p.run(make_task(), trigger="pre-eval")
        step = result["stages"]["reviewers"]["steps"][0]
        assert step["passed"] is False
        assert "requires a 'role'" in step["error"]

    def test_reviewer_failure_does_not_kill_parallel_stage(self, tmp_workdir):
        router = review_router(raises_role="security_reviewer")
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        result = p.run(task, trigger="pre-eval")

        stage = result["stages"]["reviewers"]
        by_id = {s["id"]: s for s in stage["steps"]}
        # The failing lane reports the error…
        sec = by_id["security"]
        assert sec["passed"] is False
        assert "LLM call failed" in sec["error"]
        # …but the other lanes still ran and passed.
        assert by_id["runtime"]["passed"] is True
        assert by_id["contracts"]["passed"] is True
        assert stage["any_failed"] is True
        assert len(task["findings"]) == 2  # only successful lanes plumbed

    def test_step_budget_override_accepted(self, tmp_workdir):
        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "reviewers",
                        "steps": [
                            {
                                "id": "runtime",
                                "type": "review_agent",
                                "role": "runtime",
                                "budget": {"max_iterations": 50},
                            }
                        ],
                    }
                ]
            }
        }
        router = review_router()
        p = Pipeline(config, tmp_workdir, router=router)
        result = p.run(make_task(), trigger="pre-eval")
        assert result["stages"]["reviewers"]["passed"] is True


# ── (c) Sequential single-step review_agent stage ────────────────────────


class TestSequentialReviewAgentStage:
    def test_single_step_stage_runs(self, tmp_workdir):
        config = {
            "pipeline": {
                "stages": [
                    {"id": "runtime_only", "type": "review_agent", "role": "runtime"}
                ]
            }
        }
        router = review_router()
        p = Pipeline(config, tmp_workdir, router=router)
        task = make_task()
        result = p.run(task, trigger="pre-eval")
        stage = result["stages"]["runtime_only"]
        assert stage["passed"] is True
        assert stage["steps"][0]["type"] == "review_agent"
        assert stage["steps"][0]["data"]["role"] == "runtime_reviewer"
        assert router.roles == ["runtime_reviewer"]


# ── (d) R2.12: task criteria/intent into Lane B prompts ────────────────────


class TestReviewAgentIntentContext:
    """_run_review_agent builds an intent block from the task dict (§10)."""

    def test_task_criteria_built_into_intent_block(self, tmp_workdir):
        """task['criteria'] reaches every reviewer as an intent section."""
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        task["criteria"] = [
            "zero-dollar promotional orders are accepted",
            "Stripe is not contacted for free orders",
        ]
        p.run(task, trigger="pre-eval")
        for role in DEFAULT_REVIEWER_ROLES:
            user_prompt = router.clients[role].messages_seen[0][1]["content"]
            assert "Task intent (developer criteria):" in user_prompt
            assert "review-task" in user_prompt  # task id rendered
            assert "zero-dollar promotional orders are accepted" in user_prompt
            assert "Stripe is not contacted for free orders" in user_prompt
            # Defect context still present alongside intent.
            assert "Evidence store:" in user_prompt

    def test_explicit_intent_context_wins_over_criteria(self, tmp_workdir):
        """task['intent_context'] takes precedence over raw criteria."""
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        task["criteria"] = ["ignored criteria string"]
        task["intent_context"] = (
            "Task intent (developer criteria):\n"
            "- [github-842] (in_progress) Allow promotional orders\n"
            "    - zero-dollar promotional orders are accepted"
        )
        p.run(task, trigger="pre-eval")
        user_prompt = router.clients["runtime_reviewer"].messages_seen[0][1]["content"]
        assert "github-842" in user_prompt
        assert "zero-dollar promotional orders are accepted" in user_prompt
        assert "ignored criteria string" not in user_prompt

    def test_structured_intent_context_list_rendered(self, tmp_workdir):
        """task['intent_context'] as {id,title,criteria,status} dicts renders."""
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        task["intent_context"] = [
            {
                "id": "github-842",
                "title": "Allow promotional orders with zero-dollar total",
                "criteria": ["Stripe is not contacted for free orders"],
                "status": "in_progress",
            },
        ]
        p.run(task, trigger="pre-eval")
        user_prompt = router.clients["security_reviewer"].messages_seen[0][1]["content"]
        assert "Task intent (developer criteria):" in user_prompt
        assert "github-842" in user_prompt
        assert "Stripe is not contacted for free orders" in user_prompt

    def test_no_criteria_no_intent_section(self, tmp_workdir):
        """A task without criteria/intent gets no intent section (Lane A only)."""
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        p.run(make_task(), trigger="pre-eval")  # make_task() has criteria: []
        user_prompt = router.clients["runtime_reviewer"].messages_seen[0][1]["content"]
        assert "Task intent" not in user_prompt
        assert "Evidence store:" in user_prompt
