"""
R2.8 Adversarial verifier — Lane C: finding verification (DESIGN_v2.md §9, §12).

Each candidate finding produced by the Lane-B reviewers (R2.7) is processed
**independently** by a :class:`VerifierAgent` whose system prompt frames the
task as falsification: *"Your job is to disprove F19."* The agent gets
execution tools (``run_command`` / ``read_file`` / ``search_pattern`` —
DESIGN_v2.md §9: "already present") plus the R2.5 codeintel tools
(:func:`engine.agents.tools.codeintel_tools`), and must attempt to refute
the claim using them. A finding is reported ``CONFIRMED`` **only if the
claim survives the agent's best attempts to break it**; otherwise
``REFUTED`` (a concrete reason the claim is false) or ``UNVERIFIED`` (could
not settle it either way).

The output schema (:class:`VerifierFindings`) carries the §12 structured
factors — impact, patch_causality, reproducible, execution_path_confirmed,
verifier_confidence, developer_relevance — plus the verdict. The
**deterministic** gate :func:`BLOCK` (pure, no LLM) turns those factors into
a block decision, so no model ever gets to judge a fuzzy ``7.9 vs 8.1``
score (DESIGN_v2.md §12: "Don't trust LLM CVE scores as the gate").

The execution sandbox (DESIGN_v2.md §9 ⚠️ — ephemeral container/microVM for
PR service mode, R2.11) is **optional**: pass ``sandbox=`` to
:class:`VerifierAgent` and its ``run_command`` tool executes inside the
sandbox (engine/github/sandbox.py) instead of on the host. Without a
sandbox, ``run_command`` executes on the host exactly like the evaluator's
``_tool_run_command`` — local mode unchanged.

NOTE: no ``from __future__ import annotations`` here — the schema dataclasses
must carry real types so ``parse_response`` can instantiate the nested
``VerifierFinding`` objects.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from engine.agents import AgentRunner, Budget
from engine.agents.runner import RoleRouter
from engine.agents.schemas import schema_to_prompt
from engine.agents.tools import Tool, codeintel_tools, make_read_file_tool
from engine.github.sandbox import Sandbox, SandboxError, SandboxTimeoutError

__all__ = [
    "VerifierFinding",
    "VerifierFindings",
    "VerifierCandidate",
    "VerifierAgent",
    "BLOCK",
]

#: Verdicts a verifier may assign.
VERDICTS = ("CONFIRMED", "REFUTED", "UNVERIFIED")

#: Patch-causality levels (DESIGN_v2.md §12).
PATCH_CAUSALITIES = ("confirmed", "unconfirmed", "refuted")

#: Impact levels — same scale as reviewer severities.
IMPACTS = ("critical", "high", "medium", "low", "info")

#: Developer-relevance levels.
DEVELOPER_RELEVANCES = ("high", "medium", "low")

#: The deterministic block gate (DESIGN_v2.md §12) — a refuted or
#: unconfirmed-causality finding can never block, regardless of confidence.
BLOCK_CONFIDENCE_THRESHOLD = 0.90


@dataclass
class VerifierFinding:
    """The verifier's structured verdict on one candidate finding (§12 shape).

    Every field feeds the deterministic :func:`BLOCK` policy — the model
    produces structured factors, not a fuzzy numeric score.
    """

    finding_id: str = field(
        metadata={"description": "Id of the candidate finding this verdict refers to, e.g. F19."}
    )
    impact: str = field(
        metadata={
            "description": (
                "Impact of the defect IF the claim is true: critical, high, "
                "medium, low, or info (not how sure you are)."
            )
        }
    )
    patch_causality: str = field(
        metadata={
            "description": (
                "confirmed: the defect is caused by the patch under review; "
                "unconfirmed: unclear; refuted: the defect pre-exists the patch "
                "or is unrelated to it."
            )
        }
    )
    verdict: str = field(
        metadata={
            "description": (
                "CONFIRMED only if the claim survived your falsification "
                "attempts; REFUTED if you found concrete evidence the claim is "
                "false; UNVERIFIED if you could not settle it either way."
            )
        }
    )
    reproducible: bool = field(
        default=False,
        metadata={"description": "Whether you could reproduce the defect (repro run, test)."},
    )
    execution_path_confirmed: bool = field(
        default=False,
        metadata={
            "description": (
                "Whether you traced the actual execution path from trigger to "
                "impact (callers, guards, data flow) — not just spotted "
                "suspicious code."
            )
        },
    )
    verifier_confidence: float = field(
        default=0.0,
        metadata={"description": "Your confidence in the verdict, 0.0 to 1.0."},
    )
    developer_relevance: str = field(
        default="medium",
        metadata={
            "description": (
                "How relevant the finding is to the developer fixing this "
                "patch: high, medium, or low."
            )
        },
    )
    notes: str = field(
        default="",
        metadata={
            "description": (
                "Justification: name the evidence and tool results that "
                "refuted or supported the claim."
            )
        },
    )

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict (for pipeline step data)."""
        return {
            "finding_id": self.finding_id,
            "impact": self.impact,
            "patch_causality": self.patch_causality,
            "reproducible": self.reproducible,
            "execution_path_confirmed": self.execution_path_confirmed,
            "verifier_confidence": self.verifier_confidence,
            "developer_relevance": self.developer_relevance,
            "verdict": self.verdict,
            "notes": self.notes,
        }


@dataclass
class VerifierFindings:
    """Structured output of one verifier run: verdicts + a summary."""

    findings: list[VerifierFinding] = field(
        default_factory=list,
        metadata={
            "description": (
                "The verdicts, one per candidate finding, each with the §12 "
                "structured factors. Usually exactly one for a single-candidate run."
            )
        },
    )
    summary: str = field(
        default="",
        metadata={
            "description": (
                "One-paragraph summary of this verification run's outcome."
            )
        },
    )


@dataclass
class VerifierCandidate:
    """One candidate finding to verify (DESIGN_v2.md §9 shape)."""

    finding_id: str = field(
        metadata={"description": "Candidate finding id, e.g. F19."}
    )
    file: str = field(
        metadata={
            "description": "Path of the file the finding refers to, relative to repo root."
        }
    )
    claim: str = field(
        metadata={
            "description": (
                "The defect claim to disprove, e.g. 'Deleted users can reach "
                "this dereference with user=None.'"
            )
        }
    )
    line: int | None = field(
        default=None,
        metadata={"description": "Line number in the file, if applicable."},
    )
    trigger: str = field(
        default="",
        metadata={
            "description": (
                "The concrete input/action that would trigger the defect, "
                "e.g. 'refresh token belonging to deleted account'."
            )
        },
    )
    evidence: list[str] = field(
        default_factory=list,
        metadata={
            "description": (
                "Evidence store refs backing the claim, e.g. ['E12', 'E33', 'E52']."
            )
        },
    )
    verification_plan: list[str] = field(
        default_factory=list,
        metadata={
            "description": (
                "Steps the verifier should attempt to refute the claim, "
                "e.g. ['inspect caller guard', 'run targeted regression test']."
            )
        },
    )


def BLOCK(finding: VerifierFinding) -> bool:
    """Deterministic gate (DESIGN_v2.md §12) — pure, no LLM.

    A candidate finding blocks the merge only when ALL of:

    - ``impact`` is ``critical`` or ``high``
    - ``patch_causality`` is ``confirmed`` (the patch introduced it)
    - ``execution_path_confirmed`` (the trigger→impact path was traced)
    - ``verifier_confidence >= 0.90``

    A refuted or unconfirmed-causality finding never blocks, and confidence
    ``0.89`` does not block while ``0.90`` does — the boundary is exact.
    """
    return (
        finding.impact in {"critical", "high"}
        and finding.patch_causality == "confirmed"
        and finding.execution_path_confirmed
        and finding.verifier_confidence >= BLOCK_CONFIDENCE_THRESHOLD
    )


#: System prompt shared by all verifier runs — the falsification framing.
_SYSTEM_PROMPT_TEMPLATE = """\
You are the adversarial verifier in a code-review ensemble (GitReins 2.0, \
Lane C: finding verification). Your job is to DISPROVE the candidate finding \
below — not to confirm it, and not to fix the code.

Treat the candidate as guilty until proven innocent. Use the available tools \
(run_command, read_file, search_pattern, code-intel queries) to ATTEMPT TO \
REFUTE the claim: inspect the caller guard, check the callee's return \
contract, trace the execution path from trigger to impact, run the targeted \
regression test. Report CONFIRMED only if the claim SURVIVES your best \
attempts to break it.

For each candidate, produce the structured factors (DESIGN_v2.md §12):
- impact: critical, high, medium, low, or info — the severity of the defect
  IF the claim is true (not how sure you are).
- patch_causality: confirmed if the defect is caused by the patch under
  review; unconfirmed if unclear; refuted if the defect pre-exists the patch
  or is unrelated to it.
- reproducible: whether you could actually reproduce the defect.
- execution_path_confirmed: whether you traced the real execution path from
  trigger to impact (callers, guards, data flow) — not just spotted
  suspicious code.
- verifier_confidence: 0.0..1.0 — your confidence in the verdict after the
  falsification attempts.
- developer_relevance: high, medium, or low — how relevant the finding is to
  the developer fixing this patch.
- verdict: CONFIRMED (claim survived refutation), REFUTED (you found
  concrete evidence the claim is false: a guard, a contract, a passing
  test), or UNVERIFIED (could not settle it either way).
- notes: justify the verdict — name the evidence ids and tool results that
  refuted or supported the claim.

{output_schema}
"""


def serialize_candidate(candidate: VerifierCandidate) -> str:
    """Render a candidate finding as one prompt-friendly block (§9 shape).

    Includes the finding id, file:line, claim, trigger, evidence refs and the
    verification plan — everything the verifier needs to attack the claim.
    """
    loc = f"{candidate.file}:{candidate.line}" if candidate.line is not None else candidate.file
    parts = [f"Finding ID: {candidate.finding_id}", f"Location: {loc}", f"Claim: {candidate.claim}"]
    if candidate.trigger:
        parts.append(f"Trigger: {candidate.trigger}")
    if candidate.evidence:
        parts.append("Evidence refs: " + ", ".join(candidate.evidence))
    if candidate.verification_plan:
        plan_lines = "\n".join(
            f"  {i}. {step}" for i, step in enumerate(candidate.verification_plan, 1)
        )
        parts.append(f"Verification plan:\n{plan_lines}")
    return "\n".join(parts)


# ── Verifier tool belt ───────────────────────────────────────────────────


def _make_run_command_tool(
    workdir: str, timeout: int = 30, sandbox: Sandbox | None = None
) -> Tool:
    """Build the run_command Tool bound to ``workdir``.

    Mirrors AgenticEvaluator._tool_run_command (DESIGN_v2.md §9: "already
    present"). With ``sandbox`` (R2.11, PR service mode) the command runs
    inside the ephemeral container via ``sandbox.run`` — never on the host;
    without one it runs ``subprocess.run(cmd, shell=True, cwd=workdir)``
    directly on the host (local trusted mode, unchanged).
    """

    def _run_command(cmd: str = "", command: str | None = None) -> dict:
        cmd = cmd or command or ""
        if not cmd:
            return {"error": "No command provided"}
        try:
            if sandbox is not None:
                result = sandbox.run(cmd, cwd=sandbox.cwd, timeout=timeout)
                exit_code, output = result.exit_code, result.stdout + result.stderr
            else:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=workdir,
                )
                exit_code, output = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            return {"cmd": cmd, "error": f"Command timed out after {timeout}s"}
        except SandboxTimeoutError:
            return {"cmd": cmd, "error": f"Command timed out after {timeout}s"}
        except SandboxError as e:
            return {"cmd": cmd, "error": str(e)}
        except Exception as e:  # noqa: BLE001 — tool boundary: report, don't crash
            return {"cmd": cmd, "error": str(e)}
        if len(output) > 4000:
            output = output[:4000] + f"\n... [truncated, exit_code={exit_code}]"
        return {"cmd": cmd, "exit_code": exit_code, "output": output}

    return Tool(
        name="run_command",
        description=(
            "Run a shell command in the repository working tree. Returns exit code, "
            "stdout, and stderr. Use for tests, lint, build, repro. Do NOT re-run the "
            "same command."
        ),
        parameters={
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": "Shell command to run."}},
            "required": ["cmd"],
        },
        fn=_run_command,
    )


def _make_search_pattern_tool(workdir: str, timeout: int = 60) -> Tool:
    """Build the search_pattern Tool bound to ``workdir``.

    Mirrors AgenticEvaluator._tool_search_pattern: ripgrep primary path,
    GNU grep fallback, pure-Python last resort. Returns file:line matches
    capped at 200.
    """

    def _search_pattern(regex: str, file_glob: str = "*") -> dict:
        try:
            re.compile(regex)
        except re.error as e:
            return {"error": f"Invalid regex: {e}", "matches": [], "count": 0}

        if rg_path := shutil.which("rg"):
            return _search_with_rg(regex, file_glob, rg_path)
        if grep_path := shutil.which("grep"):
            return _search_with_grep(regex, file_glob, grep_path)
        return _search_with_python(regex, file_glob)

    def _search_with_rg(regex: str, file_glob: str, rg_path: str) -> dict:
        cmd = [
            rg_path,
            "--line-number",
            "--no-heading",
            "--smart-case",
            "-e",
            regex,
            "--glob",
            "!__pycache__/**",
            "--glob",
            "!.git/**",
            "--glob",
            "!venv/**",
            "--glob",
            "!.venv/**",
            "--glob",
            "!node_modules/**",
            "--glob",
            "!.pytest_cache/**",
        ]
        if file_glob != "*":
            cmd.extend(["--glob", file_glob])
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir
            )
            if result.returncode == 2 and "No files were searched" in result.stderr:
                return {"matches": [], "count": 0}
            if result.returncode > 1:
                return {"error": "rg failed", "matches": [], "count": 0}
            lines = result.stdout.strip().split("\n") if result.stdout else []
            return {"matches": lines[:200], "count": min(len(lines), 200)}
        except subprocess.TimeoutExpired:
            if grep_path := shutil.which("grep"):
                return _search_with_grep(regex, file_glob, grep_path)
            return {"error": "rg timed out", "matches": [], "count": 0}
        except Exception:  # noqa: BLE001 — tool boundary
            return {"error": "rg failed", "matches": [], "count": 0}

    def _search_with_grep(regex: str, file_glob: str, grep_path: str) -> dict:
        cmd = [grep_path, "-rnI", "--exclude-dir=__pycache__", "-e", regex, "."]
        if file_glob != "*":
            cmd = [
                grep_path,
                "-rnI",
                "--exclude-dir=__pycache__",
                f"--include={file_glob}",
                "-e",
                regex,
                ".",
            ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir
            )
            lines = result.stdout.strip().split("\n") if result.stdout else []
            return {"matches": lines[:200], "count": min(len(lines), 200)}
        except subprocess.TimeoutExpired:
            return _search_with_python(regex, file_glob)
        except Exception:  # noqa: BLE001 — tool boundary
            return _search_with_python(regex, file_glob)

    def _search_with_python(regex: str, file_glob: str = "*") -> dict:
        import fnmatch

        matches: list[str] = []
        try:
            pattern = re.compile(regex)
        except re.error as e:
            return {"error": f"Invalid regex: {e}", "matches": [], "count": 0}
        skip_dirs = {
            ".git", "venv", ".venv", "node_modules", "__pycache__",
            ".gitreins-sandbox", ".pytest_cache",
        }
        for root, dirs, files in os.walk(workdir):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            for fname in files:
                if file_glob != "*" and not fnmatch.fnmatch(fname, file_glob):
                    continue
                path = os.path.join(root, fname)
                try:
                    with open(path, "r", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if pattern.search(line):
                                rel = os.path.relpath(path, workdir)
                                matches.append(f"{rel}:{lineno}: {line.rstrip()[:200]}")
                                if len(matches) >= 200:
                                    return {"matches": matches, "count": 200}
                except (OSError, UnicodeDecodeError):
                    continue
        return {"matches": matches, "count": len(matches)}

    return Tool(
        name="search_pattern",
        description=(
            "Search the codebase for a regex pattern. Returns matching file:line "
            "entries (max 200). Do NOT repeat the same regex search."
        ),
        parameters={
            "type": "object",
            "properties": {
                "regex": {"type": "string", "description": "Python regex pattern."},
                "file_glob": {
                    "type": "string",
                    "description": "Optional: glob to filter files (e.g., '*.py').",
                },
            },
            "required": ["regex"],
        },
        fn=_search_pattern,
    )


def _verifier_tools(
    workdir: str, command_timeout: int = 30, sandbox: Sandbox | None = None
) -> list[Tool]:
    """The verifier's tool belt (DESIGN_v2.md §9).

    run_command / read_file / search_pattern — the "already present"
    execution tools — plus the R2.5 codeintel tools. With ``sandbox`` (R2.11,
    PR service mode) run_command executes inside the sandbox instead of on
    the host. Sandbox tools (sandbox_read/sandbox_write) are auto-injected by
    AgentRunner.
    """
    return [
        _make_run_command_tool(workdir, timeout=command_timeout, sandbox=sandbox),
        make_read_file_tool(workdir),
        _make_search_pattern_tool(workdir),
        *codeintel_tools(workdir),
    ]


class VerifierAgent(AgentRunner):
    """Adversarial verifier for one candidate finding (R2.8, DESIGN_v2.md §9).

    ``run(candidate)`` drives the generic bounded agent loop with the
    falsification system prompt ("Your job is to disprove F<n>"), the
    execution + codeintel tool belt, the :class:`VerifierFindings` output
    schema, and ``model_role='verifier'`` (resolved from the
    ``review.models.verifier`` config block; absent → env-default client,
    never raises). ``sandbox`` (R2.11, PR service mode) routes the
    run_command tool through the ephemeral container
    (engine/github/sandbox.py) instead of the host; without it, local host
    execution is unchanged.
    """

    MODEL_ROLE = "verifier"

    def __init__(
        self,
        router: RoleRouter | None = None,
        *,
        sandbox: Sandbox | None = None,
        **kwargs,
    ):
        """``sandbox``: optional :class:`~engine.github.sandbox.Sandbox` —
        when set, run_command executes inside it (PR service mode, R2.11);
        when None, run_command runs on the host (local mode, unchanged).

        Stored as ``_exec_sandbox`` because ``AgentRunner.sandbox`` is
        already taken by the scratch-state property (sandbox_read/write)."""
        super().__init__(router=router, **kwargs)
        self._exec_sandbox = sandbox

    @property
    def exec_sandbox(self) -> Sandbox | None:
        """The execution sandbox, if any (PR service mode, R2.11)."""
        return self._exec_sandbox

    def run(
        self,
        candidate: VerifierCandidate | dict,
        *,
        budget: Budget | None = None,
    ) -> VerifierFindings:
        """Verify (attempt to disprove) one candidate finding.

        Args:
            candidate: The candidate finding to attack — a
                :class:`VerifierCandidate` (or an equivalent dict).
            budget: Agent Budget; defaults to an unlimited Budget.

        Returns:
            A typed :class:`VerifierFindings` — usually exactly one verdict.
        """
        cand = (
            candidate
            if isinstance(candidate, VerifierCandidate)
            else VerifierCandidate(**candidate)
        )
        user_prompt = (
            "Candidate finding to verify — your job is to DISPROVE it:\n\n"
            + serialize_candidate(cand)
            + "\n\nAttempt to refute the claim with the available tools "
            "(inspect the guard, check the contract, trace the path, run the "
            "repro). Then produce your structured verdict: CONFIRMED only if "
            "the claim survived your falsification attempts."
        )

        return super().run(
            system_prompt=_SYSTEM_PROMPT_TEMPLATE.format(
                output_schema=schema_to_prompt(VerifierFindings)
            ),
            user_prompt=user_prompt,
            tools=_verifier_tools(
                self.workdir,
                command_timeout=self.command_timeout,
                sandbox=self._exec_sandbox,
            ),
            output_schema=VerifierFindings,
            model_role=self.MODEL_ROLE,
            budget=budget if budget is not None else Budget(),
        )
