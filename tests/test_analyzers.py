"""
R2.4 tests for engine/analyzers — external security scanners as evidence producers.

Covers: semgrep / ast-grep / trivy / gitleaks JSON parsers against canned
fixtures (check ids, lines, payloads), the gitleaks secret-redaction
guarantee, parse_analyzer_output dispatch, run_analyzers aggregation, and
graceful [] when a binary is absent or the runner exits nonzero.
"""

import json

import pytest

from engine.analyzers import (
    parse_astgrep_json,
    parse_gitleaks_json,
    parse_semgrep_json,
    parse_trivy_json,
    run_astgrep,
    run_gitleaks,
    run_semgrep,
    run_trivy,
)
from engine.evidence.producers import parse_analyzer_output, run_analyzers

# ── fixtures ─────────────────────────────────────────────────────────────

SEMGREP_JSON = {
    "results": [
        {
            "check_id": "python.lang.security.audit.dangerous-system-call.dangerous-system-call",
            "path": "main.py",
            "start": {"line": 41},
            "end": {"line": 41},
            "extra": {"message": "Detected dangerous system call", "severity": "WARNING"},
        },
        {
            "check_id": "python.lang.correctness.useless-eq.useless-eq",
            "path": "main.py",
            "start": {"line": 12},
            "end": {"line": 14},
            "extra": {"message": "Found useless comparison", "severity": "ERROR"},
        },
    ],
    "errors": [],
}

ASTGREP_JSON = [
    {
        "text": "os.system(cmd)",
        "file": "src/main.py",
        "range": {"start": {"line": 7, "column": 0}, "end": {"line": 7, "column": 16}},
        "rule": {"id": "dangerous-system-call"},
        "severity": "warning",
        "message": "os.system is dangerous",
    },
    {
        "text": "eval(input())",
        "file": "src/main.py",
        "range": {"start": {"line": 22, "column": 4}, "end": {"line": 22, "column": 16}},
        "rule": {"id": "avoid-eval"},
        "severity": "error",
        "message": "Do not use eval",
    },
]

TRIVY_JSON = {
    "Results": [
        {
            "Target": "Pipfile.lock",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2024-1234",
                    "PkgName": "requests",
                    "InstalledVersion": "2.31.0",
                    "FixedVersion": "2.31.1",
                    "Severity": "HIGH",
                    "Title": "requests: HTTP request smuggling",
                    "PkgPath": "Pipfile.lock",
                }
            ],
        },
        {
            "Target": "requirements.txt",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2024-5678",
                    "PkgName": "pyyaml",
                    "InstalledVersion": "6.0.0",
                    "FixedVersion": "6.0.1",
                    "Severity": "CRITICAL",
                    "Title": "pyyaml: arbitrary code execution",
                }
            ],
        },
    ]
}


def _fake_secret() -> str:
    """Build a realistic-looking secret at runtime so the literal never appears
    in source (the Tier-1 secrets guard scans committed files)."""
    return "ghp_" + "".join(chr(ord("a") + (i % 26)) for i in range(36))


def _gitleaks_json(secret: str) -> list[dict]:
    return [
        {
            "RuleID": "generic-api-key",
            "Description": "Detected a Generic API Key",
            "File": "config/settings.py",
            "StartLine": 12,
            "EndLine": 12,
            "Secret": secret,
            "Match": f"api_key = '{secret}'",
            "Severity": "HIGH",
        },
        {
            "RuleID": "aws-access-token",
            "Description": "Detected AWS Access Token",
            "File": "deploy/iam.py",
            "StartLine": 34,
            "EndLine": 35,
            "Secret": secret,
            "Match": f"aws_secret = '{secret}'",
            "Severity": "MEDIUM",
        },
    ]


class _FakeResult:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── semgrep ──────────────────────────────────────────────────────────────


def test_parse_semgrep_json_two_findings():
    evs = parse_semgrep_json(SEMGREP_JSON)
    assert len(evs) == 2
    assert [e.id for e in evs] == ["static:semgrep:1", "static:semgrep:2"]
    assert all(e.kind == "static_analysis" for e in evs)
    assert all(e.source == "semgrep" for e in evs)

    first = evs[0]
    assert first.file == "main.py"
    assert first.line_start == 41
    assert first.line_end == 41
    assert first.payload["code"] == SEMGREP_JSON["results"][0]["check_id"]
    assert first.payload["message"] == "Detected dangerous system call"
    assert first.payload["severity"] == "WARNING"
    assert first.payload["tool"] == "semgrep"

    second = evs[1]
    assert second.line_start == 12
    assert second.line_end == 14
    assert second.payload["severity"] == "ERROR"


def test_parse_semgrep_json_empty_results():
    assert parse_semgrep_json({}) == []
    assert parse_semgrep_json({"results": []}) == []


# ── ast-grep ─────────────────────────────────────────────────────────────


def test_parse_astgrep_json_list():
    evs = parse_astgrep_json(ASTGREP_JSON)
    assert len(evs) == 2
    assert [e.id for e in evs] == ["static:ast-grep:1", "static:ast-grep:2"]
    assert all(e.kind == "static_analysis" for e in evs)
    assert all(e.source == "ast-grep" for e in evs)

    first = evs[0]
    assert first.file == "src/main.py"
    assert first.line_start == 7
    assert first.line_end == 7
    assert first.payload["code"] == "dangerous-system-call"
    assert first.payload["severity"] == "warning"
    assert first.payload["tool"] == "ast-grep"

    assert evs[1].payload["code"] == "avoid-eval"
    assert evs[1].payload["message"] == "Do not use eval"
    assert evs[1].line_start == 22


def test_parse_astgrep_json_accepts_dict_with_matches():
    assert len(parse_astgrep_json({"matches": ASTGREP_JSON})) == 2
    assert parse_astgrep_json({}) == []


# ── trivy ────────────────────────────────────────────────────────────────


def test_parse_trivy_json_two_vulnerabilities():
    evs = parse_trivy_json(TRIVY_JSON)
    assert len(evs) == 2
    assert [e.id for e in evs] == ["static:trivy:1", "static:trivy:2"]
    assert all(e.kind == "static_analysis" for e in evs)
    assert all(e.source == "trivy" for e in evs)

    first = evs[0]
    assert first.payload["code"] == "CVE-2024-1234"
    assert first.payload["message"] == "CVE-2024-1234 requests: HTTP request smuggling"
    assert first.payload["severity"] == "HIGH"
    assert first.payload["package"] == "requests"
    assert first.payload["installed_version"] == "2.31.0"
    assert first.payload["fixed_version"] == "2.31.1"
    assert first.payload["tool"] == "trivy"

    second = evs[1]
    assert second.payload["code"] == "CVE-2024-5678"
    assert second.payload["severity"] == "CRITICAL"


def test_parse_trivy_json_file_uses_pkgpath_else_target():
    evs = parse_trivy_json(TRIVY_JSON)
    assert evs[0].file == "Pipfile.lock"  # PkgPath present
    assert evs[1].file == "requirements.txt"  # falls back to Target
    assert evs[0].line_start is None
    assert evs[0].line_end is None


def test_parse_trivy_json_empty_results():
    assert parse_trivy_json({}) == []
    assert parse_trivy_json({"Results": [{"Target": "x", "Vulnerabilities": []}]}) == []


# ── gitleaks ─────────────────────────────────────────────────────────────


def test_parse_gitleaks_json_secret_redacted():
    secret = _fake_secret()
    evs = parse_gitleaks_json(_gitleaks_json(secret))
    assert len(evs) == 2
    for ev in evs:
        assert ev.payload["secret"] == "<redacted>"
        assert ev.payload["secret"] != secret
        # The raw secret must never appear anywhere in the serialized evidence
        # (file, message, payload, refs) — the guard scans for secrets.
        assert secret not in json.dumps(ev.to_dict())


def test_parse_gitleaks_json_fields_and_lines():
    evs = parse_gitleaks_json(_gitleaks_json(_fake_secret()))
    assert [e.id for e in evs] == ["static:gitleaks:1", "static:gitleaks:2"]
    assert all(e.kind == "static_analysis" for e in evs)
    assert all(e.source == "gitleaks" for e in evs)

    first = evs[0]
    assert first.payload["code"] == "generic-api-key"
    assert first.payload["message"] == "Detected a Generic API Key"
    assert first.payload["severity"] == "HIGH"
    assert first.payload["tool"] == "gitleaks"
    assert first.file == "config/settings.py"
    assert first.line_start == 12
    assert first.line_end == 12

    assert evs[1].file == "deploy/iam.py"
    assert evs[1].line_start == 34
    assert evs[1].line_end == 35


def test_parse_gitleaks_json_accepts_findings_dict():
    data = {"findings": _gitleaks_json(_fake_secret())}
    assert len(parse_gitleaks_json(data)) == 2
    assert parse_gitleaks_json({}) == []


# ── parse_analyzer_output dispatch ───────────────────────────────────────


@pytest.mark.parametrize(
    "tool,data,parse_fn",
    [
        ("semgrep", SEMGREP_JSON, parse_semgrep_json),
        ("ast-grep", ASTGREP_JSON, parse_astgrep_json),
        ("trivy", TRIVY_JSON, parse_trivy_json),
        ("gitleaks", _gitleaks_json(_fake_secret()), parse_gitleaks_json),
    ],
)
def test_parse_analyzer_output_dispatch(tool, data, parse_fn):
    assert parse_analyzer_output(tool, data) == parse_fn(data)


def test_parse_analyzer_output_astgrep_alias():
    assert parse_analyzer_output("astgrep", ASTGREP_JSON) == parse_astgrep_json(ASTGREP_JSON)


def test_parse_analyzer_output_unknown_tool_raises():
    with pytest.raises(ValueError):
        parse_analyzer_output("bandit", {})


# ── run_* graceful degradation ───────────────────────────────────────────


def test_run_semgrep_absent_binary_returns_empty(monkeypatch):
    monkeypatch.setattr("engine.analyzers.semgrep.shutil.which", lambda name: None)
    assert run_semgrep(".") == []


def test_run_astgrep_absent_binary_returns_empty(monkeypatch):
    monkeypatch.setattr("engine.analyzers.astgrep.shutil.which", lambda name: None)
    assert run_astgrep(".") == []


def test_run_trivy_absent_binary_returns_empty(monkeypatch):
    monkeypatch.setattr("engine.analyzers.trivy.shutil.which", lambda name: None)
    assert run_trivy(".") == []


def test_run_gitleaks_absent_binary_returns_empty(monkeypatch):
    monkeypatch.setattr("engine.analyzers.gitleaks.shutil.which", lambda name: None)
    assert run_gitleaks(".") == []


# ── run_* parse stdout on nonzero exit ───────────────────────────────────


def test_run_semgrep_parses_output_on_nonzero_exit(monkeypatch):
    # semgrep exits 1 when rules match — findings must still be parsed.
    monkeypatch.setattr("engine.analyzers.semgrep.shutil.which", lambda name: "/usr/bin/semgrep")
    monkeypatch.setattr(
        "engine.analyzers.semgrep.subprocess.run",
        lambda *a, **k: _FakeResult(1, json.dumps(SEMGREP_JSON), ""),
    )
    evs = run_semgrep("/tmp/work")
    assert len(evs) == 2
    assert evs[0].payload["code"] == SEMGREP_JSON["results"][0]["check_id"]


def test_run_gitleaks_parses_output_on_finding_exit(monkeypatch):
    # gitleaks exits 1 when leaks are found — findings must still be parsed
    # and the secret must still be redacted.
    monkeypatch.setattr("engine.analyzers.gitleaks.shutil.which", lambda name: "/usr/bin/gitleaks")
    monkeypatch.setattr(
        "engine.analyzers.gitleaks.subprocess.run",
        lambda *a, **k: _FakeResult(1, json.dumps(_gitleaks_json(_fake_secret())), ""),
    )
    evs = run_gitleaks("/tmp/work")
    assert len(evs) == 2
    assert evs[0].payload["secret"] == "<redacted>"


def test_run_trivy_parses_output_on_clean_exit(monkeypatch):
    monkeypatch.setattr("engine.analyzers.trivy.shutil.which", lambda name: "/usr/bin/trivy")
    monkeypatch.setattr(
        "engine.analyzers.trivy.subprocess.run",
        lambda *a, **k: _FakeResult(0, json.dumps(TRIVY_JSON), ""),
    )
    evs = run_trivy("/tmp/work")
    assert len(evs) == 2
    assert evs[0].payload["code"] == "CVE-2024-1234"


# ── run_analyzers aggregation ────────────────────────────────────────────


def test_run_analyzers_concatenates_tools(monkeypatch):
    monkeypatch.setattr(
        "engine.analyzers.semgrep.run_semgrep",
        lambda workdir: parse_semgrep_json(SEMGREP_JSON)[:1],
    )
    monkeypatch.setattr(
        "engine.analyzers.astgrep.run_astgrep",
        lambda workdir: parse_astgrep_json(ASTGREP_JSON)[:1],
    )
    monkeypatch.setattr(
        "engine.analyzers.trivy.run_trivy",
        lambda workdir: parse_trivy_json(TRIVY_JSON)[:1],
    )
    monkeypatch.setattr(
        "engine.analyzers.gitleaks.run_gitleaks",
        lambda workdir: parse_gitleaks_json(_gitleaks_json(_fake_secret()))[:1],
    )
    result = run_analyzers("/tmp/work")
    assert [e.source for e in result] == ["semgrep", "ast-grep", "trivy", "gitleaks"]


def test_run_analyzers_respects_tools_subset(monkeypatch):
    monkeypatch.setattr(
        "engine.analyzers.semgrep.run_semgrep",
        lambda workdir: parse_semgrep_json(SEMGREP_JSON)[:1],
    )
    monkeypatch.setattr(
        "engine.analyzers.trivy.run_trivy",
        lambda workdir: parse_trivy_json(TRIVY_JSON)[:1],
    )
    result = run_analyzers("/tmp/work", tools=("semgrep", "trivy"))
    assert [e.source for e in result] == ["semgrep", "trivy"]


def test_run_analyzers_unknown_tool_skipped(monkeypatch):
    monkeypatch.setattr(
        "engine.analyzers.semgrep.run_semgrep",
        lambda workdir: parse_semgrep_json(SEMGREP_JSON)[:1],
    )
    assert run_analyzers("/tmp/work", tools=("bandit",)) == []
    assert [e.source for e in run_analyzers("/tmp/work", tools=("semgrep", "bandit"))] == [
        "semgrep"
    ]
