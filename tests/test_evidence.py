"""
R2.4 tests for engine/evidence — models, store, provenance, producers.

Covers: dataclass construction + 12-kind enum, id auto-assignment, append/
query filters, get/all, to_dict/from_dict round-trip, thread safety,
evidence_for ref resolution, provenance record + resolve_ref (all four ref
formats), and producers against a real mypy-format StaticDiag.
"""

import threading

import pytest

from engine.evidence import (
    EVIDENCE_KINDS,
    Evidence,
    EvidenceKind,
    EvidenceStore,
    Provenance,
    format_evidence_id,
    from_command,
    from_static_diag,
    make_ref,
    record,
    resolve_ref,
    static_findings_to_evidence,
)
from engine.evidence.models import KIND_TO_PREFIX, PREFIX_TO_KIND
from engine.static_analysis import StaticDiag, _parse_mypy

ALL_KINDS = {
    "diff",
    "symbol",
    "reference",
    "call_edge",
    "static_analysis",
    "lsp",
    "test",
    "command",
    "history",
    "requirement",
    "repo_rule",
    "ci",
}


def make_evidence(**overrides):
    base = {
        "id": "",
        "kind": "diff",
        "source": "git",
        "file": "auth/session.py",
        "line_start": 10,
        "line_end": 12,
        "payload": {"summary": "rotated token"},
    }
    base.update(overrides)
    return Evidence(**base)


@pytest.fixture
def store():
    return EvidenceStore()


# ── models ──────────────────────────────────────────────────────────────


def test_evidence_construction_all_fields():
    ev = make_evidence(
        id="E17",
        kind="static_analysis",
        source="semgrep",
        file="auth/session.py",
        line_start=181,
        line_end=185,
        payload={"code": "A1", "message": "x"},
    )
    assert ev.id == "E17"
    assert ev.kind == "static_analysis"
    assert ev.source == "semgrep"
    assert ev.file == "auth/session.py"
    assert ev.line_start == 181
    assert ev.line_end == 185
    assert ev.payload == {"code": "A1", "message": "x"}


def test_evidence_kind_enum_covers_all_12_kinds():
    assert len(EvidenceKind) == 12
    assert len(EVIDENCE_KINDS) == 12
    assert {k.value for k in EvidenceKind} == ALL_KINDS
    assert set(EVIDENCE_KINDS) == ALL_KINDS
    # Every kind has a ref prefix and every prefix maps back to a kind.
    assert set(KIND_TO_PREFIX) == ALL_KINDS
    assert set(PREFIX_TO_KIND) == set(KIND_TO_PREFIX.values())
    assert len(PREFIX_TO_KIND) == 12


def test_format_evidence_id():
    assert format_evidence_id(1) == "E1"
    assert format_evidence_id(17) == "E17"
    assert format_evidence_id(44) == "E44"


def test_make_ref_static_style():
    assert make_ref("static_analysis", "semgrep", "17") == "static:semgrep:17"
    assert make_ref("call_edge", "caller", "Session.refresh") == "graph:caller:Session.refresh"
    assert make_ref("reference", "auth/session.py", "181") == "source:auth/session.py:181"
    assert make_ref("test", "verification", "F19") == "test:verification:F19"


def test_to_dict_from_dict_roundtrip():
    ev = make_evidence(id="E17", payload={"code": "arg-type", "message": "boom"})
    d = ev.to_dict()
    assert d["kind"] == "diff"
    assert d["id"] == "E17"
    assert d["payload"] == {"code": "arg-type", "message": "boom"}
    ev2 = Evidence.from_dict(d)
    assert ev2 == ev
    assert ev2.to_dict() == d


def test_from_dict_missing_keys_raises():
    with pytest.raises(KeyError):
        Evidence.from_dict({"id": "E1", "kind": "diff"})


def test_from_dict_unknown_kind_raises():
    with pytest.raises(ValueError):
        Evidence.from_dict(make_evidence(id="E1", kind="bogus").to_dict())


# ── store: append / get / all / query ──────────────────────────────────


def test_append_assigns_monotonic_ids(store):
    e1 = store.append(make_evidence())
    e2 = store.append(make_evidence())
    e3 = store.append(
        make_evidence(kind="test", source="pytest", file=None, line_start=None, line_end=None, payload={})
    )
    assert e1.id == "E1"
    assert e2.id == "E2"
    assert e3.id == "E3"
    assert store.get("E1") is e1
    assert store.get("E3") is e3


def test_append_accepts_kwargs(store):
    ev = store.append(kind="command", source="git", file=None, line_start=None, line_end=None, payload={"exit_code": 0})
    assert ev.id == "E1"
    assert ev.kind == "command"
    assert store.get("E1") is ev


def test_append_duplicate_id_raises(store):
    store.append(make_evidence(id="E5"))
    with pytest.raises(KeyError):
        store.append(make_evidence(id="E5"))


def test_append_explicit_ref_id_preserved(store):
    ev = store.append(make_evidence(id="static:semgrep:17"))
    assert ev.id == "static:semgrep:17"
    assert store.get("static:semgrep:17") is ev


def test_get_missing_returns_none(store):
    assert store.get("E999") is None
    assert store.get("nope") is None


def test_all_returns_append_order(store):
    e1 = store.append(make_evidence())
    e2 = store.append(
        make_evidence(kind="command", source="git", file=None, line_start=None, line_end=None, payload={"exit_code": 0})
    )
    e3 = store.append(make_evidence(kind="test", source="pytest", file=None, line_start=None, line_end=None, payload={}))
    assert store.all() == [e1, e2, e3]


def test_query_filters(store):
    e1 = store.append(make_evidence(kind="static_analysis", source="semgrep", file="a.py"))
    e2 = store.append(make_evidence(kind="static_analysis", source="mypy", file="a.py"))
    e3 = store.append(make_evidence(kind="diff", source="git", file="b.py"))
    assert store.query(kind="static_analysis") == [e1, e2]
    assert store.query(source="semgrep") == [e1]
    assert store.query(file="a.py") == [e1, e2]
    assert store.query(kind="static_analysis", source="mypy", file="a.py") == [e2]
    assert store.query(kind="diff", source="semgrep") == []
    assert store.query(id=e2.id) == [e2]
    # EvidenceKind enum works as a filter too
    assert store.query(kind=EvidenceKind.STATIC_ANALYSIS) == [e1, e2]


def test_thread_safety_concurrent_appends():
    store = EvidenceStore()
    n_threads = 8
    per_thread = 25

    def worker():
        for _ in range(per_thread):
            store.append(make_evidence())

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    items = store.all()
    assert len(items) == n_threads * per_thread
    ids = [e.id for e in items]
    assert len(set(ids)) == len(ids)  # no duplicates under concurrency
    assert ids == [f"E{i}" for i in range(1, len(ids) + 1)]


# ── store: evidence_for ref resolution ─────────────────────────────────


def test_evidence_for_resolves_refs(store):
    e1 = store.append(make_evidence())
    sa1 = store.append(make_evidence(kind="static_analysis", source="semgrep", file="a.py", payload={"code": "A1"}))
    sa2 = store.append(make_evidence(kind="static_analysis", source="semgrep", file="b.py", payload={"code": "A2"}))
    cmd = store.append(
        make_evidence(kind="command", source="verifier", id="verification-F19", payload={"exit_code": 0})
    )
    graph = store.append(make_evidence(kind="call_edge", source="caller", payload={"key": "Session.refresh"}))

    resolved = store.evidence_for(
        ["E1", "static:semgrep:2", "test:verification-F19", "graph:caller:Session.refresh"]
    )
    assert resolved == [e1, sa2, cmd, graph]


def test_evidence_for_source_file_ref(store):
    ev = store.append(
        make_evidence(kind="reference", source="rg", file="auth/session.py", line_start=181, line_end=185, payload={})
    )
    assert store.evidence_for(["source:auth/session.py:181"]) == [ev]
    assert store.evidence_for(["source:auth/session.py:183"]) == [ev]  # inside span
    assert store.evidence_for(["source:auth/session.py:200"]) == []  # outside span


def test_evidence_for_unresolvable_skipped(store):
    store.append(make_evidence())
    assert store.evidence_for(["E1", "E99", "static:semgrep:17"]) == [store.get("E1")]


def test_evidence_for_strict_raises(store):
    store.append(make_evidence())
    with pytest.raises(KeyError):
        store.evidence_for(["E1", "E99"], strict=True)


# ── provenance ─────────────────────────────────────────────────────────


def test_provenance_record_and_lookup(store):
    store.append(make_evidence(id="E1"))
    store.append(make_evidence(id="E2"))
    prov = record(
        store,
        id="F19",
        claim="session tokens must be rotated",
        evidence_refs=["E1", "static:semgrep:17"],
        generated_by={"role": "runtime_reviewer", "model": "deepseek-v4-flash"},
        verified_by={"model": "qwen3.7-flash", "verdict": "confirmed"},
    )
    assert prov.id == "F19"
    assert prov.claim == "session tokens must be rotated"
    assert store.provenance() == [prov]
    assert store.provenance("F19") == [prov]  # by record id
    assert store.provenance("E1") == [prov]  # by cited evidence
    assert store.provenance("E2") == []
    d = prov.to_dict()
    assert d["evidence_refs"] == ["E1", "static:semgrep:17"]
    assert d["generated_by"] == {"role": "runtime_reviewer", "model": "deepseek-v4-flash"}
    assert d["verified_by"] == {"model": "qwen3.7-flash", "verdict": "confirmed"}


def test_provenance_auto_id_and_roundtrip(store):
    p1 = record(store, evidence_refs=["E1"], generated_by={"role": "r", "model": "m"}, verified_by={"model": "v", "verdict": "confirmed"})
    p2 = record(store, evidence_refs=["E2"], generated_by={"role": "r", "model": "m"}, verified_by={"model": "v", "verdict": "rejected"})
    assert p1.id == "P1"
    assert p2.id == "P2"
    assert store.provenance() == [p1, p2]
    restored = Provenance.from_dict(p1.to_dict())
    assert restored == p1


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("static:semgrep:17", {"kind": "static_analysis", "source": "semgrep", "ordinal": 17}),
        ("graph:caller:Session.refresh", {"kind": "call_edge", "source": "caller", "key": "Session.refresh"}),
        ("source:auth/session.py:181", {"kind": "reference", "file": "auth/session.py", "line": 181}),
        ("test:verification-F19", {"kind": "test", "id": "verification-F19"}),
        ("E17", {"id": "E17"}),
    ],
)
def test_resolve_ref_formats(ref, expected):
    assert resolve_ref(ref) == expected


def test_resolve_ref_malformed():
    with pytest.raises(ValueError):
        resolve_ref("static:semgrep")  # missing ordinal
    with pytest.raises(ValueError):
        resolve_ref("static:semgrep:abc")  # non-integer ordinal
    with pytest.raises(ValueError):
        resolve_ref("source:auth/session.py:notaline")
    with pytest.raises(ValueError):
        resolve_ref("")
    with pytest.raises(ValueError):
        resolve_ref(None)


# ── producers ──────────────────────────────────────────────────────────


def test_from_static_diag_real_mypy():
    text = (
        'main.py:9: error: Argument 1 to "get_user" has incompatible '
        'type "str"; expected "int"  [arg-type]\n'
    )
    diags = _parse_mypy(text)
    assert len(diags) == 1
    assert isinstance(diags[0], StaticDiag)
    ev = from_static_diag(diags[0], "mypy")
    assert ev.kind == "static_analysis"
    assert ev.source == "mypy"
    assert ev.file == "main.py"
    assert ev.line_start == 9
    assert ev.line_end == 9
    assert ev.payload["code"] == "arg-type"
    assert 'incompatible type "str"' in ev.payload["message"]
    assert ev.payload["severity"] == "error"
    assert ev.payload["tool"] == "mypy"
    assert ev.id == ""  # left for the store to assign


def test_from_static_diag_ordinal_ref():
    diag = StaticDiag(file="main.py", line=9, severity="error", message="x", code="arg-type", tool="mypy")
    ev = from_static_diag(diag, "mypy", ordinal=17)
    assert ev.id == "static:mypy:17"


def test_static_findings_to_evidence():
    findings = [
        {
            "file": "main.py",
            "line": 9,
            "severity": "error",
            "message": 'Argument 1 to "get_user" has incompatible type "str"',
            "code": "arg-type",
            "tool": "mypy",
        },
        {
            "file": "main.py",
            "line": 15,
            "severity": "warning",
            "message": "Returning Any from function declared to return str",
            "code": "no-any-return",
            "tool": "mypy",
        },
    ]
    evs = static_findings_to_evidence(findings, "mypy")
    assert len(evs) == 2
    assert [e.id for e in evs] == ["static:mypy:1", "static:mypy:2"]
    assert all(e.kind == "static_analysis" for e in evs)
    assert all(e.source == "mypy" for e in evs)
    assert evs[0].file == "main.py"
    assert evs[0].line_start == 9
    assert evs[0].payload["code"] == "arg-type"
    assert evs[0].payload["severity"] == "error"
    assert evs[1].payload["code"] == "no-any-return"
    assert evs[1].payload["severity"] == "warning"


def test_static_findings_to_evidence_store_resolution():
    findings = [
        {"file": "main.py", "line": 9, "severity": "error", "message": "a", "code": "arg-type", "tool": "mypy"},
        {"file": "main.py", "line": 15, "severity": "warning", "message": "b", "code": "no-any-return", "tool": "mypy"},
    ]
    store = EvidenceStore()
    for ev in static_findings_to_evidence(findings, "mypy"):
        store.append(ev)
    assert store.evidence_for(["static:mypy:2"]) == [store.all()[1]]
    assert store.evidence_for(["static:mypy:1"]) == [store.all()[0]]


def test_from_command():
    ev = from_command(
        name="pytest",
        command="uv run pytest tests/test_evidence.py -x -q",
        exit_code=0,
        stdout="3 passed",
        stderr="",
        file="tests/test_evidence.py",
        line_start=1,
    )
    assert ev.kind == "command"
    assert ev.source == "pytest"
    assert ev.file == "tests/test_evidence.py"
    assert ev.line_start == 1
    assert ev.payload == {
        "name": "pytest",
        "command": "uv run pytest tests/test_evidence.py -x -q",
        "exit_code": 0,
        "stdout": "3 passed",
        "stderr": "",
    }


# ── persistence ────────────────────────────────────────────────────────


def test_persistence_replay(tmp_path):
    path = str(tmp_path / "evidence.json")
    store = EvidenceStore(path)
    store.append(make_evidence())
    store.append(
        make_evidence(kind="command", source="git", file=None, line_start=None, line_end=None, payload={"exit_code": 0})
    )
    record(store, evidence_refs=["E1"], generated_by={"role": "r", "model": "m"}, verified_by={"model": "v", "verdict": "confirmed"})

    reloaded = EvidenceStore(path)
    assert reloaded.all() == store.all()
    assert reloaded.provenance() == store.provenance()
    # The counter survives the reload: next auto id continues the sequence.
    assert reloaded.append(make_evidence()).id == "E3"
