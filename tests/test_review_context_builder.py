"""
R2.6 tests for engine/review/context_builder — EvidencePlanner.

Covers: execution of every supported retrieval type against a fake
in-memory CodeIntelProvider (dispatch + limit pass-through), cap
enforcement (callers of foo → 3 callers), unsupported-type degradation,
provider-error degradation (never raises), evidence kind mapping
(call_edge/reference/symbol), file/line extraction, and the evidence
append/query round-trip through the EvidenceStore.

Hermetic: fake provider only — no network, no real code-intel backend.
"""

from engine.evidence import EvidenceStore
from engine.review import ChangedSymbol, EvidencePlanner, RetrievalRequest, ScoutPlan

ALL_TYPES = [
    "callers",
    "references",
    "definition",
    "implementations",
    "callees",
    "symbols",
]


class FakeCodeIntelProvider:
    """In-memory CodeIntelProvider stand-in: canned dicts per method."""

    def __init__(self, results=None, raises=None):
        self.results = results or {}
        self.raises = raises or {}
        self.calls = []  # (method, query, limit)

    def _record(self, name, query, limit):
        self.calls.append((name, query, limit))
        if name in self.raises:
            raise self.raises[name]
        return list(self.results.get(name, []))

    def callers(self, query, file_path=None, symbol=None, limit=10):
        return self._record("callers", query, limit)

    def references(self, query, file_path=None, symbol=None, limit=10):
        return self._record("references", query, limit)

    def definition(self, query, file_path=None, symbol=None, limit=10):
        return self._record("definition", query, limit)

    def implementations(self, query, file_path=None, symbol=None, limit=10):
        return self._record("implementations", query, limit)

    def callees(self, query, file_path=None, symbol=None, limit=10):
        return self._record("callees", query, limit)

    def symbols(self, query, file_path=None, symbol=None, limit=10):
        return self._record("symbols", query, limit)

    # Unused protocol methods (R2.5 base) — satisfy the full shape.
    def contract(self, *args, **kwargs):
        return []

    def impact(self, *args, **kwargs):
        return []

    def history(self, *args, **kwargs):
        return []

    def prs(self, *args, **kwargs):
        return []

    def cross_repo(self, *args, **kwargs):
        return []


def hit(file="a.py", line=1, **extra):
    d = {"file": file, "line": line}
    d.update(extra)
    return d


def make_plan(requests, lenses=None):
    """Build a ScoutPlan with the given (type, query) retrieval requests."""
    return ScoutPlan(
        changed_symbols=[ChangedSymbol(symbol="SessionManager.rotate_token", risk="high")],
        retrieval_requests=[RetrievalRequest(type=t, query=q) for t, q in requests],
        review_lenses=lenses or ["state/concurrency", "authorization"],
    )


# ── (a) Execution / dispatch ────────────────────────────────────────────


class TestExecution:
    def test_each_type_dispatches_to_provider_method(self):
        provider = FakeCodeIntelProvider()
        planner = EvidencePlanner(provider, default_limit=3)
        plan = make_plan(
            [
                ("callers", "foo"),
                ("references", "bar"),
                ("definition", "baz"),
                ("implementations", "qux"),
                ("callees", "quux"),
                ("symbols", "corge"),
            ]
        )
        planner.execute(plan)
        assert [c[0] for c in provider.calls] == ALL_TYPES
        assert [c[1] for c in provider.calls] == ["foo", "bar", "baz", "qux", "quux", "corge"]
        assert all(c[2] == 3 for c in provider.calls)  # default limit passed through

    def test_cap_enforcement(self):
        """callers of foo → 3 callers (default_limit), not all 10."""
        provider = FakeCodeIntelProvider(
            {"callers": [hit(f"c{i}.py") for i in range(10)]}
        )
        store = EvidenceStore()
        planner = EvidencePlanner(provider, store, default_limit=3)
        evs = planner.execute(make_plan([("callers", "foo")]))
        assert len(evs) == 3
        assert len(store.all()) == 3
        assert provider.calls[0][2] == 3  # provider asked for the cap too

    def test_per_execute_limit_override(self):
        provider = FakeCodeIntelProvider({"references": [hit() for _ in range(5)]})
        planner = EvidencePlanner(provider, default_limit=3)
        evs = planner.execute(make_plan([("references", "r")]), limit=2)
        assert len(evs) == 2
        assert provider.calls[0][2] == 2

    def test_unsupported_type_skipped_never_raises(self):
        provider = FakeCodeIntelProvider()
        planner = EvidencePlanner(provider, EvidenceStore())
        plan = make_plan([("bogus", "x"), ("callers", "foo")])
        evs = planner.execute(plan)
        assert evs == []  # bogus type produced nothing
        assert provider.calls == [("callers", "foo", 3)]  # others still ran

    def test_provider_error_degrades_and_others_continue(self):
        provider = FakeCodeIntelProvider(
            {"callers": [hit()]},
            raises={"references": RuntimeError("boom")},
        )
        planner = EvidencePlanner(provider, EvidenceStore(), default_limit=3)
        plan = make_plan([("references", "bad"), ("callers", "ok")])
        evs = planner.execute(plan)
        assert len(evs) == 1
        assert evs[0].payload["query"] == "ok"  # the failing one was skipped

    def test_non_list_provider_result_degrades(self):
        provider = FakeCodeIntelProvider({"callers": "not a list"})
        planner = EvidencePlanner(provider, EvidenceStore())
        assert planner.execute(make_plan([("callers", "foo")])) == []

    def test_non_dict_entries_filtered(self):
        provider = FakeCodeIntelProvider({"callers": [hit(), "junk", None, 42]})
        planner = EvidencePlanner(provider, EvidenceStore())
        evs = planner.execute(make_plan([("callers", "foo")]))
        assert len(evs) == 1


# ── (b) Evidence shape / kind mapping ───────────────────────────────────


class TestEvidenceShape:
    def test_kind_mapping(self):
        provider = FakeCodeIntelProvider({t: [hit()] for t in ALL_TYPES})
        store = EvidenceStore()
        planner = EvidencePlanner(provider, store, default_limit=3)
        plan = make_plan([(t, f"q-{t}") for t in ALL_TYPES])
        planner.execute(plan)
        all_ev = store.all()
        assert len(all_ev) == len(ALL_TYPES)
        kinds = {e.kind for e in all_ev}
        assert kinds == {"call_edge", "reference", "symbol"}
        assert all(e.source == "codeintel" for e in all_ev)
        # per-type kind: callers+callees → call_edge, references → reference, rest → symbol
        by_query = {e.payload["query"]: e for e in all_ev}
        assert by_query["q-callers"].kind == "call_edge"
        assert by_query["q-callees"].kind == "call_edge"
        assert by_query["q-references"].kind == "reference"
        assert by_query["q-definition"].kind == "symbol"
        assert by_query["q-implementations"].kind == "symbol"
        assert by_query["q-symbols"].kind == "symbol"

    def test_file_and_line_extracted(self):
        provider = FakeCodeIntelProvider(
            {
                "definition": [
                    {"file": "auth/session.py", "line_start": 41, "line_end": 45, "kind": "function"}
                ]
            }
        )
        planner = EvidencePlanner(provider, EvidenceStore())
        evs = planner.execute(make_plan([("definition", "Session.rotate")]))
        ev = evs[0]
        assert ev.file == "auth/session.py"
        assert ev.line_start == 41
        assert ev.line_end == 45
        assert ev.payload["request_type"] == "definition"
        assert ev.payload["query"] == "Session.rotate"
        assert ev.payload["result"]["kind"] == "function"

    def test_line_key_and_string_coercion(self):
        provider = FakeCodeIntelProvider(
            {"references": [{"file": "a.py", "line": "7"}]}
        )
        planner = EvidencePlanner(provider, EvidenceStore())
        evs = planner.execute(make_plan([("references", "r")]))
        assert evs[0].line_start == 7  # str coerced to int
        assert evs[0].line_end == 7  # falls back to line_start

    def test_missing_file_line_ok(self):
        provider = FakeCodeIntelProvider({"symbols": [{"name": "foo"}]})
        planner = EvidencePlanner(provider, EvidenceStore())
        evs = planner.execute(make_plan([("symbols", "foo")]))
        assert evs[0].file is None
        assert evs[0].line_start is None
        assert evs[0].line_end is None


# ── (c) Evidence store round-trip ───────────────────────────────────────


class TestStoreRoundTrip:
    def test_append_query_roundtrip(self):
        provider = FakeCodeIntelProvider(
            {
                "callers": [hit("auth/callers.py", line=10)],
                "references": [hit("auth/ref.py", line=20)],
            }
        )
        store = EvidenceStore()
        planner = EvidencePlanner(provider, store, default_limit=3)
        evs = planner.execute(make_plan([("callers", "foo"), ("references", "foo")]))
        assert store.all() == evs  # appended in execution order
        assert store.query(kind="call_edge", source="codeintel") == [evs[0]]
        assert store.query(kind="reference", source="codeintel") == [evs[1]]
        assert store.query(kind="call_edge", file="auth/callers.py") == [evs[0]]
        assert store.get(evs[0].id) is evs[0]
        assert store.evidence_for([evs[0].id, evs[1].id]) == evs

    def test_planner_creates_own_store(self):
        provider = FakeCodeIntelProvider({"callers": [hit()]})
        planner = EvidencePlanner(provider, default_limit=3)
        evs = planner.execute(make_plan([("callers", "foo")]))
        assert len(planner.store.all()) == 1
        assert evs[0].id == "E1"

    def test_empty_plan_no_evidence_no_calls(self):
        provider = FakeCodeIntelProvider()
        planner = EvidencePlanner(provider, EvidenceStore())
        plan = ScoutPlan(changed_symbols=[], retrieval_requests=[], review_lenses=[])
        assert planner.execute(plan) == []
        assert provider.calls == []
        assert planner.store.all() == []

    def test_evidence_persistence_roundtrip(self, tmp_path):
        """codeintel evidence survives store JSON replay (kinds are valid)."""
        provider = FakeCodeIntelProvider({"callers": [hit("a.py", line=5)]})
        path = str(tmp_path / "evidence.json")
        store = EvidenceStore(path)
        planner = EvidencePlanner(provider, store)
        planner.execute(make_plan([("callers", "foo")]))
        reloaded = EvidenceStore(path)
        assert reloaded.all() == store.all()
        assert reloaded.all()[0].kind == "call_edge"
