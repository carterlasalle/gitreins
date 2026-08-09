"""
R2.13 ReviewRunArchiver — rich review history + provenance (DESIGN_v2.md §13).

Persists a completed review run under ``review_runs/<commit-sha>/`` as JSON
artifacts, one file per stage:

    manifest.json          change.json          static-evidence.json
    scout.json             candidates.json       verification.json
    final-findings.json    requirements.json     usage.json

Every ``archive_*`` method writes exactly one artifact and degrades
gracefully — an archive failure is logged and skipped, never raised — and
returns the written path (or None on failure). ``archive_all`` runs the
stages that have data in a single call and returns ``{artifact: path}`` for
what was written.

``commit_sha`` defaults to ``git rev-parse HEAD`` resolved at ``base_dir``
(the repo root); when that fails (no git, unborn HEAD) the run is archived
under ``review_runs/local/``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any, Iterable, cast

from engine.review.ranker import finding_to_dict

logger = logging.getLogger("gitreins.review.history")

#: The nine artifact filenames (DESIGN_v2.md §13).
ARTIFACT_NAMES: tuple[str, ...] = (
    "manifest.json",
    "change.json",
    "static-evidence.json",
    "scout.json",
    "candidates.json",
    "verification.json",
    "final-findings.json",
    "requirements.json",
    "usage.json",
)

#: Schema tag written into every manifest.
SCHEMA = "gitreins.review-run/v1"

#: Subdirectory under ``base_dir`` holding per-sha run dirs.
RUNS_DIR = "review_runs"

#: Fallback run-dir name when no commit sha can be resolved.
LOCAL_RUN = "local"


def _utc_now() -> str:
    """Current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _asdict(obj: Any) -> dict:
    """Best-effort dataclass/dict serialization to a plain dict."""
    if isinstance(obj, dict):
        return dict(obj)
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return dict(cast(dict, to_dict()))
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)  # type: ignore[arg-type]
    return {}


def _finding_to_dict(finding: Any) -> dict:
    """Normalize a finding (ReviewFinding/VerifierFinding/dict) to a dict."""
    try:
        return finding_to_dict(finding)
    except TypeError:
        return _asdict(finding)


def _evidence_to_dict(evidence: Any) -> dict:
    """Serialize one Evidence object (or dict) to a plain dict."""
    if isinstance(evidence, dict):
        return dict(evidence)
    to_dict = getattr(evidence, "to_dict", None)
    if callable(to_dict):
        return dict(cast(dict, to_dict()))
    return _asdict(evidence)


def _write_json(path: str, data: Any) -> bool:
    """Atomically write ``data`` as sorted pretty JSON; False on any failure."""
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except Exception as exc:  # noqa: BLE001 — archive boundary: degrade, don't crash
        logger.warning("Failed to write review artifact %s: %s", path, exc)
        return False


class ReviewRunArchiver:
    """Persist review runs under ``review_runs/<commit-sha>/`` (§13).

    Args:
        base_dir: Repo root the runs live under — default ``.`` (repo-root
            ``review_runs/``). Overridable for tests and sandboxes.
        commit_sha: Sha naming the run dir; when None, resolved from
            ``git rev-parse HEAD`` at ``base_dir`` (fallback: ``local``).
    """

    def __init__(self, base_dir: str = ".", commit_sha: str | None = None):
        self.base_dir = os.path.abspath(base_dir)
        self.commit_sha = (commit_sha if commit_sha is not None else self._detect_sha()) or ""
        self.run_dir = os.path.join(self.base_dir, RUNS_DIR, self.commit_sha or LOCAL_RUN)

    # ── Artifact writers ──────────────────────────────────────────

    def archive_manifest(self, meta: dict | None = None) -> str | None:
        """Write manifest.json: schema, sha, timestamp, artifact presence map."""
        artifacts = {
            name: os.path.isfile(os.path.join(self.run_dir, name)) for name in ARTIFACT_NAMES
        }
        artifacts["manifest.json"] = True  # this file is being written right now
        data: dict[str, Any] = {
            "schema": SCHEMA,
            "commit_sha": self.commit_sha,
            "created_at": _utc_now(),
            "artifacts": artifacts,
        }
        if meta:
            data["meta"] = dict(meta)
        return self._write("manifest.json", data)

    def archive_change(
        self, changed_files: Iterable[str] | None, diff_context: str = ""
    ) -> str | None:
        """Write change.json: the files + diff context under review."""
        files = [str(f) for f in (changed_files or [])]
        return self._write(
            "change.json",
            {
                "changed_files": files,
                "file_count": len(files),
                "diff_context": diff_context or "",
            },
        )

    def archive_static_evidence(self, evidence: Any) -> str | None:
        """Write static-evidence.json: the static-analysis/LSP evidence items.

        Accepts an :class:`~engine.evidence.store.EvidenceStore` (all items
        are archived) or an iterable of Evidence objects/dicts. Callers that
        want only the static-analysis slice filter the store first and pass
        the list.
        """
        items = evidence.all() if hasattr(evidence, "all") else list(evidence or [])
        return self._write(
            "static-evidence.json",
            {"evidence": [_evidence_to_dict(e) for e in items], "count": len(items)},
        )

    def archive_scout(self, plan: Any) -> str | None:
        """Write scout.json: the ScoutPlan (symbols, retrievals, lenses)."""
        return self._write("scout.json", _asdict(plan) if plan is not None else {})

    def archive_candidates(self, candidates: Iterable[Any] | None) -> str | None:
        """Write candidates.json: reviewer findings as verification candidates."""
        items = [_finding_to_dict(c) for c in (candidates or [])]
        return self._write("candidates.json", {"candidates": items, "count": len(items)})

    def archive_verification(self, verifications: Iterable[Any] | None) -> str | None:
        """Write verification.json: verifier verdicts (VerifierFinding shape)."""
        items = [_finding_to_dict(v) for v in (verifications or [])]
        return self._write("verification.json", {"verifications": items, "count": len(items)})

    def archive_final_findings(
        self,
        findings: Iterable[Any] | None,
        *,
        verifications: Iterable[Any] | None = None,
        verifier_model: str = "",
    ) -> str | None:
        """Write final-findings.json with full §13 provenance.

        Every entry carries ``id`` (its own, or a deterministic synthetic
        ``F<n>``), the finding fields, ``generated_by`` {role, model} (stamped
        by the orchestrator at merge time), and ``verified_by`` {model,
        verdict} — merged from ``verifications`` (matched by finding id)
        when provided.
        """
        verdicts: dict[str, dict] = {}
        if verifications:
            for v in verifications:
                vd = _finding_to_dict(v)
                fid = str(vd.get("finding_id") or vd.get("id") or "")
                if fid:
                    verdicts[fid] = vd
        items: list[dict] = []
        for i, finding in enumerate(findings or [], 1):
            d = _finding_to_dict(finding)
            fid = str(d.get("finding_id") or d.get("id") or f"F{i}")
            d["id"] = fid
            d.setdefault("generated_by", {})
            verdict = verdicts.get(fid)
            if verdict is not None:
                d["verified_by"] = {"model": verifier_model, "verdict": verdict.get("verdict", "")}
            else:
                d.setdefault("verified_by", {})
            items.append(d)
        return self._write("final-findings.json", {"findings": items, "count": len(items)})

    def archive_requirements(self, requirements: Iterable[Any] | None) -> str | None:
        """Write requirements.json: task/intent requirements + requirement evidence."""
        items = [dict(r) if isinstance(r, dict) else _asdict(r) for r in (requirements or [])]
        return self._write("requirements.json", {"requirements": items, "count": len(items)})

    def archive_usage(self, usage: dict | None) -> str | None:
        """Write usage.json: DAG usage stats (roles, finding counts, errors)."""
        return self._write("usage.json", dict(usage or {}))

    # ── Batch ─────────────────────────────────────────────────────

    def archive_all(
        self,
        *,
        change: dict | None = None,
        static_evidence: Any = None,
        scout: Any = None,
        candidates: Iterable[Any] | None = None,
        verification: Iterable[Any] | None = None,
        final_findings: Iterable[Any] | None = None,
        verifications: Iterable[Any] | None = None,
        verifier_model: str = "",
        requirements: Iterable[Any] | None = None,
        usage: dict | None = None,
        manifest: dict | None = None,
    ) -> dict[str, str]:
        """Archive every stage that has data; manifest written last.

        ``change`` is a ``{"changed_files": [...], "diff_context": ...}``
        dict. Returns ``{artifact name: written path}`` for the stages that
        were archived. Never raises.
        """
        written: dict[str, str] = {}
        if change is not None:
            self._record(
                written,
                self.archive_change(
                    change.get("changed_files", []), change.get("diff_context", "")
                ),
            )
        if static_evidence is not None:
            self._record(written, self.archive_static_evidence(static_evidence))
        if scout is not None:
            self._record(written, self.archive_scout(scout))
        if candidates is not None:
            self._record(written, self.archive_candidates(candidates))
        if verification is not None:
            self._record(written, self.archive_verification(verification))
        if final_findings is not None:
            self._record(
                written,
                self.archive_final_findings(
                    final_findings,
                    verifications=verifications,
                    verifier_model=verifier_model,
                ),
            )
        if requirements is not None:
            self._record(written, self.archive_requirements(requirements))
        if usage is not None:
            self._record(written, self.archive_usage(usage))
        self._record(written, self.archive_manifest(manifest))
        return written

    @staticmethod
    def _record(written: dict[str, str], path: str | None) -> None:
        """Remember a written artifact (failures are skipped)."""
        if path:
            written[os.path.basename(path)] = path

    # ── Internals ─────────────────────────────────────────────────

    def _write(self, name: str, data: dict) -> str | None:
        """Write one artifact under the run dir; None on failure."""
        path = os.path.join(self.run_dir, name)
        return path if _write_json(path, data) else None

    def _detect_sha(self) -> str:
        """Resolve HEAD at base_dir via git; '' when unavailable."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.base_dir,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.strip()


__all__ = ["ReviewRunArchiver", "ARTIFACT_NAMES", "SCHEMA", "RUNS_DIR", "LOCAL_RUN"]
