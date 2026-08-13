"""
R2.5 tests for engine/codeintel — code-intelligence providers + agent tools.

Covers: the get_provider factory (availability + graceful ripgrep fallback),
ast-grep structural pattern search against a fixture file, ripgrep lexical
search, LSP provider graceful degradation (empty results when the server
binary is missing) plus pure response parsers with canned JSON, the graph
provider's documented no-backend behavior, serena's clean-import /
construction-time availability error, and the 8 agent tools returning small
structured dicts.

Hermetic: no network; serena's MCP client is never exercised (construction
is blocked by monkeypatching the binary lookup).
"""

import pytest

from engine.codeintel import (
    AstGrepProvider,
    CodeIntelUnavailableError,
    GraphProvider,
    LspProvider,
    RipgrepProvider,
    SerenaProvider,
    SerenaUnavailableError,
    get_provider,
)
from engine.codeintel.astgrep import parse_astgrep_matches
from engine.codeintel.lsp import parse_document_symbols, parse_lsp_locations
from engine.codeintel.ripgrep import parse_rg_json
from engine.codeintel.tools import make_codeintel_tools
from engine.agents.tools import ToolRegistry, codeintel_tools

# ── fixture helpers ─────────────────────────────────────────────────────

FIXTURE_CODE = '''\
def greet(name):
    """Greet someone."""
    return f"hello {name}"


def main():
    print(greet("world"))
    print(greet("everyone"))
    unused = 1  # noqa
'''

EXPECTED_TOOLS = [
    "ast_search",
    "text_search",
    "get_symbol_definition",
    "find_references",
    "get_callers",
    "get_callees",
    "get_implementations",
    "get_change_impact",
]


def _call(tool, *args, **kwargs):
    """Invoke a Tool's fn, asserting it is bound (tests only)."""
    assert tool.fn is not None
    return tool.fn(*args, **kwargs)


def write_fixture(tmp_path):
    src = tmp_path / "fixture.py"
    src.write_text(FIXTURE_CODE)
    return tmp_path


# ── provider factory ────────────────────────────────────────────────────


def test_factory_ripgrep_always_available(tmp_path):
    provider = get_provider("ripgrep", workdir=str(tmp_path))
    assert isinstance(provider, RipgrepProvider)


def test_factory_graph_always_available(tmp_path):
    provider = get_provider("graph", workdir=str(tmp_path))
    assert isinstance(provider, GraphProvider)
    result = provider.impact("anything")
    assert result and result[0]["status"] == "no_backend"


def test_factory_unknown_kind_raises(tmp_path):
    with pytest.raises(ValueError):
        get_provider("bogus", workdir=str(tmp_path))


def test_factory_astgrep_available_when_binary_present(tmp_path, monkeypatch):
    monkeypatch.setattr(AstGrepProvider, "available", classmethod(lambda cls: True))
    provider = get_provider("astgrep", workdir=str(tmp_path))
    assert isinstance(provider, AstGrepProvider)


def test_factory_astgrep_falls_back_to_ripgrep(tmp_path, monkeypatch):
    monkeypatch.setattr(AstGrepProvider, "available", classmethod(lambda cls: False))
    provider = get_provider("astgrep", workdir=str(tmp_path))
    assert isinstance(provider, RipgrepProvider)


def test_factory_lsp_falls_back_to_ripgrep(tmp_path, monkeypatch):
    monkeypatch.setattr(LspProvider, "available", classmethod(lambda cls, tool=None: False))
    provider = get_provider("lsp", workdir=str(tmp_path))
    assert isinstance(provider, RipgrepProvider)


def test_factory_serena_falls_back_to_ripgrep(tmp_path, monkeypatch):
    monkeypatch.setattr(SerenaProvider, "available", classmethod(lambda cls: False))
    provider = get_provider("serena", workdir=str(tmp_path))
    assert isinstance(provider, RipgrepProvider)


# ── ast-grep structural search ──────────────────────────────────────────


def test_astgrep_pattern_match_on_fixture(tmp_path):
    write_fixture(tmp_path)
    provider = AstGrepProvider(workdir=str(tmp_path))
    matches = provider.search("print($$$)", limit=10)
    assert len(matches) >= 2
    for m in matches:
        assert set(m) >= {"file", "line_start", "line_end", "pattern", "snippet"}
        assert m["pattern"] == "print($$$)"
        assert m["line_start"] >= 1
    assert any("fixture.py" in m["file"] for m in matches)


def test_astgrep_search_respects_limit(tmp_path):
    write_fixture(tmp_path)
    provider = AstGrepProvider(workdir=str(tmp_path))
    matches = provider.search("$X", limit=3)
    assert len(matches) <= 3


def test_astgrep_parse_canned_json():
    raw = (
        '[{"file": "a.py", "range": {"start": {"line": 6}, "end": {"line": 6}},'
        ' "text": "print(x)"}]'
    )
    matches = parse_astgrep_matches(raw, pattern="print($$$)")
    assert len(matches) == 1
    assert matches[0]["line_start"] == 7  # 0-based → 1-based
    assert matches[0]["snippet"] == "print(x)"


def test_astgrep_parse_bad_json_returns_empty():
    assert parse_astgrep_matches("not json", "x") == []


def test_astgrep_unavailable_binary_raises_at_construction(tmp_path, monkeypatch):
    monkeypatch.setattr("engine.codeintel.astgrep.shutil.which", lambda name: None)
    with pytest.raises(CodeIntelUnavailableError):
        AstGrepProvider(workdir=str(tmp_path))


# ── ripgrep lexical search ──────────────────────────────────────────────


def test_ripgrep_text_search_fixture(tmp_path):
    write_fixture(tmp_path)
    provider = RipgrepProvider(workdir=str(tmp_path))
    matches = provider.text_search("greet", limit=10)
    assert len(matches) >= 3  # def + 2 call sites
    for m in matches:
        assert set(m) >= {"file", "line", "snippet", "pattern"}
        assert m["pattern"] == "greet"
        assert m["line"] >= 1
    assert any("fixture.py" in m["file"] for m in matches)


def test_ripgrep_parse_canned_ndjson():
    raw = (
        '{"type":"begin","data":{"path":{"text":"./a.py"}}}\n'
        '{"type":"match","data":{"path":{"text":"./a.py"},"line_number":4,'
        '"lines":{"text":"    print(greet(\\"x\\"))\\n"}}}\n'
        '{"type":"summary","data":{}}\n'
    )
    matches = parse_rg_json(raw, pattern="greet")
    assert len(matches) == 1
    assert matches[0]["file"] == "./a.py"
    assert matches[0]["line"] == 4
    assert "greet" in matches[0]["snippet"]


def test_ripgrep_unavailable_binary_raises_at_construction(tmp_path, monkeypatch):
    monkeypatch.setattr("engine.codeintel.ripgrep.shutil.which", lambda name: None)
    with pytest.raises(CodeIntelUnavailableError):
        RipgrepProvider(workdir=str(tmp_path))


# ── LSP provider graceful degradation ───────────────────────────────────


def test_lsp_degrades_to_empty_when_tool_missing(tmp_path, monkeypatch):
    write_fixture(tmp_path)
    monkeypatch.setattr("engine.codeintel.lsp.find_lsp_tool", lambda tool: None)
    provider = LspProvider(workdir=str(tmp_path), tool="pylsp")
    assert provider.symbols("greet", file_path=str(tmp_path / "fixture.py")) == []
    assert provider.definition("greet", file_path=str(tmp_path / "fixture.py")) == []
    assert provider.references("greet", file_path=str(tmp_path / "fixture.py")) == []
    assert provider.callers("greet", file_path=str(tmp_path / "fixture.py")) == []
    assert provider.callees("greet", file_path=str(tmp_path / "fixture.py")) == []
    assert provider.implementations("greet", file_path=str(tmp_path / "fixture.py")) == []
    assert provider.contract("greet") == []
    assert provider.history("greet") == []
    assert provider.prs("greet") == []
    assert provider.cross_repo("greet") == []


def test_lsp_parse_document_symbols_hierarchical():
    payload = [
        {
            "name": "greet",
            "kind": 12,  # function
            "range": {"start": {"line": 0}, "end": {"line": 2}},
            "selectionRange": {"start": {"line": 0}, "end": {"line": 0}},
            "children": [],
        }
    ]
    symbols = parse_document_symbols(payload)
    assert symbols == [{"file": "", "line": 1, "kind": "function", "name": "greet"}]


def test_lsp_parse_document_symbols_flat_with_uri():
    payload = [
        {
            "name": "main",
            "kind": 12,
            "location": {
                "uri": "file:///tmp/fixture.py",
                "range": {"start": {"line": 5}, "end": {"line": 8}},
            },
        }
    ]
    symbols = parse_document_symbols(payload)
    assert symbols[0]["file"] == "/tmp/fixture.py"
    assert symbols[0]["line"] == 6


def test_lsp_parse_locations_definition():
    result = {
        "uri": "file:///tmp/fixture.py",
        "range": {"start": {"line": 0}, "end": {"line": 2}},
    }
    locs = parse_lsp_locations(result, kind="definition")
    assert locs == [
        {
            "file": "/tmp/fixture.py",
            "line_start": 1,
            "line_end": 3,
            "kind": "definition",
        }
    ]


def test_lsp_parse_locations_links():
    result = [
        {
            "targetUri": "file:///tmp/fixture.py",
            "targetRange": {"start": {"line": 9}, "end": {"line": 9}},
        }
    ]
    locs = parse_lsp_locations(result, kind="reference")
    assert locs[0]["line_start"] == 10
    assert locs[0]["kind"] == "reference"


def test_lsp_no_file_path_returns_empty(tmp_path):
    provider = LspProvider(workdir=str(tmp_path), tool="pylsp")
    assert provider.symbols("x") == []
    assert provider.definition("x") == []
    assert provider.references("x") == []


# ── graph provider (interface-only) ─────────────────────────────────────


def test_graph_provider_returns_documented_empty(tmp_path):
    provider = GraphProvider(workdir=str(tmp_path))
    for method in [
        "definition",
        "references",
        "callers",
        "callees",
        "implementations",
        "symbols",
        "contract",
        "impact",
        "history",
        "prs",
        "cross_repo",
        "get_cross_repo_impact",
    ]:
        result = getattr(provider, method)("anything")
        assert result and result[0].get("status") == "no_backend"


# ── get_cross_repo_impact (R2.14) ────────────────────────────────────────


def test_degraded_providers_return_empty_cross_repo_impact(tmp_path, monkeypatch):
    """astgrep/ripgrep/lsp/serena cannot answer cross-repo graph queries.

    Only the graph provider can (eventually) resolve cross-repo impact;
    the others degrade to ``[]`` like their ``cross_repo`` methods.
    """
    write_fixture(tmp_path)
    monkeypatch.setattr("engine.codeintel.lsp.find_lsp_tool", lambda tool: None)

    astgrep = AstGrepProvider(workdir=str(tmp_path))
    ripgrep = RipgrepProvider(workdir=str(tmp_path))
    lsp = LspProvider(workdir=str(tmp_path), tool="pylsp")
    serena = SerenaProvider.__new__(SerenaProvider)  # bypass binary check

    for provider in (astgrep, ripgrep, lsp, serena):
        assert provider.get_cross_repo_impact("greet") == []
        assert (
            provider.get_cross_repo_impact("greet", file_path="fixture.py", symbol="greet", limit=5)
            == []
        )


# ── serena provider ─────────────────────────────────────────────────────


def test_serena_module_imports_cleanly():
    import engine.codeintel.serena as serena_mod

    assert hasattr(serena_mod, "SerenaProvider")
    assert hasattr(serena_mod, "SerenaUnavailableError")


def test_serena_raises_at_construction_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("engine.codeintel.serena.shutil.which", lambda name: None)
    with pytest.raises(SerenaUnavailableError):
        SerenaProvider(workdir=str(tmp_path))


def test_serena_availability_false_when_binary_missing(monkeypatch):
    monkeypatch.setattr("engine.codeintel.serena.shutil.which", lambda name: None)
    assert SerenaProvider.available() is False


# ── agent tools ─────────────────────────────────────────────────────────


def test_tools_registry_contains_eight_tools(tmp_path):
    write_fixture(tmp_path)
    tools = codeintel_tools(base_dir=str(tmp_path))
    assert [t.name for t in tools] == EXPECTED_TOOLS

    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    for name in EXPECTED_TOOLS:
        assert name in registry
        assert registry.lookup(name) is not None
    assert len(registry.all()) == 8


def test_tools_return_structured_dicts(tmp_path):
    write_fixture(tmp_path)
    tools = codeintel_tools(base_dir=str(tmp_path))
    by_name = {t.name: t for t in tools}

    text = _call(by_name["text_search"], "greet", limit=5)
    assert isinstance(text, list)
    assert all(isinstance(m, dict) for m in text)
    assert any("file" in m for m in text)

    ast = _call(by_name["ast_search"], "print($$$)", limit=5)
    assert isinstance(ast, list)
    assert all(isinstance(m, dict) for m in ast)

    definition = _call(by_name["get_symbol_definition"], "greet", limit=5)
    assert isinstance(definition, list)
    assert all(isinstance(m, dict) for m in definition)

    impact = _call(by_name["get_change_impact"], "greet", limit=5)
    assert isinstance(impact, list)
    assert all(isinstance(m, dict) for m in impact)


def test_tools_cap_snippets_and_limits(tmp_path):
    write_fixture(tmp_path)
    tools = make_codeintel_tools(workdir=str(tmp_path))
    by_name = {t.name: t for t in tools}
    text = _call(by_name["text_search"], "greet", limit=2)
    assert len(text) <= 2
    for m in text:
        assert len(m.get("snippet", "")) <= 500 + 3  # truncation margin


def test_tools_never_raise_on_bad_args(tmp_path):
    tools = make_codeintel_tools(workdir=str(tmp_path))
    by_name = {t.name: t for t in tools}
    # Query with no matches → empty list, not an exception.
    assert _call(by_name["text_search"], "zzz_nothing_matches_zzz", limit=5) == []
    assert _call(by_name["ast_search"], "zzz_nothing_matches_zzz", limit=5) == []


def test_tools_schemas_are_openai_function_format(tmp_path):
    tools = make_codeintel_tools(workdir=str(tmp_path))
    for tool in tools:
        schema = tool.to_llm_schema()
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == tool.name
        assert fn["parameters"]["type"] == "object"
        assert "query" in fn["parameters"]["properties"]
