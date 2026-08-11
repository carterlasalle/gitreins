"""Dedicated tests for guard result types."""

from dataclasses import FrozenInstanceError

import pytest

from engine.types import GuardResult, Tier1Result


@pytest.mark.parametrize(
    ("name", "output", "detail"),
    [
        ("secrets", "", " — clean"),
        ("lint", "", " — ok"),
        ("go_lint", "", " — ok"),
        ("go_build", "", " — ok"),
        ("go_vet", "", " — ok"),
        ("tests", "3 passed", " — passed"),
        ("go_tests", "ok package/name", " — passed"),
        ("tests", "no tests collected", ""),
        ("custom", "ok", ""),
    ],
)
def test_guard_result_pass_detail(name, output, detail):
    result = GuardResult(name=name, passed=True, output=output)

    assert result._pass_detail() == detail


def test_guard_result_defaults_and_frozen_contract():
    result = GuardResult(name="lint", passed=True)

    assert result.output == ""
    assert result.error == ""
    with pytest.raises(FrozenInstanceError):
        setattr(result, "passed", False)


def test_tier1_summary_formats_passes_failures_and_empty_output():
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult("secrets", True),
            GuardResult("tests", True, "2 PASSED"),
            GuardResult("lint", False, "E501 line too long\nsecond line"),
            GuardResult("custom", False, error="command failed"),
        ],
    )

    assert result.summary == "\n".join(
        [
            "  ✓ secrets — clean",
            "  ✓ tests — passed",
            "  ✗ lint — second line",
            "  ✗ custom",
        ]
    )


def test_tier1_summary_counts_failed_lines_and_shows_tail():
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult(
                "tests",
                False,
                "intro\nFAILED tests/test_one.py\nFAIL package/two\nignored",
            )
        ],
    )

    assert result.summary == "  ✗ tests — 2 failure(s); ignored"


def test_tier1_summary_truncates_long_tail_line():
    result = Tier1Result(
        passed=False,
        results=[GuardResult("lint", False, "x" * 101)],
    )

    assert result.summary == f"  ✗ lint — {'x' * 97}..."


def test_tier1_summary_shows_failure_tail_not_first_line():
    # pytest-style output: the first line is a session banner, the error is at
    # the end — the summary must show the tail, not the banner.
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult(
                "lint",
                False,
                "=== test session starts ===\n...\nERROR: the real failure is at the end",
            )
        ],
    )

    assert result.summary == "  ✗ lint — ERROR: the real failure is at the end"


def test_tier1_summary_combines_failure_count_with_tail():
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult(
                "tests",
                False,
                "=== test session starts ===\n\nFAILED tests/test_x.py::test_y - AssertionError: boom",
            )
        ],
    )

    assert result.summary == (
        "  ✗ tests — 1 failure(s); FAILED tests/test_x.py::test_y - AssertionError: boom"
    )


def test_tier1_summary_secrets_guard_includes_gitleaks_file_line():
    output = (
        "Finding:    1\n"
        "Secret:     REDACTED\n"
        "RuleID:     github-pat\n"
        "File:       src/config.py\n"
        "Line:       12\n"
        "Commit:     deadbeef\n"
        "\n"
        "Finding:    2\n"
        "Secret:     REDACTED\n"
        "File:       .env\n"
        "Line:       3\n"
    )
    result = Tier1Result(passed=False, results=[GuardResult("secrets", False, output)])

    assert result.summary == "  ✗ secrets — 2 finding(s): src/config.py:12, .env:3"


def test_tier1_summary_secrets_findings_keep_pairs_whole_on_overflow():
    # A single pair longer than the cap is dropped whole — never cut mid-path.
    long_path = "src/" + "a" * 80 + ".py"
    output = "\n".join(f"File:       {long_path}\nLine:       {i}" for i in range(1, 4))
    result = Tier1Result(passed=False, results=[GuardResult("secrets", False, output)])

    assert result.summary == "  ✗ secrets — 3 finding(s): …"


def test_tier1_summary_secrets_without_gitleaks_fields_uses_tail():
    # Built-in scanner output has no File:/Line: fields — falls back to tail.
    output = 'Potential secrets found:\n.env:3: [hardcoded API key] value="***"'
    result = Tier1Result(passed=False, results=[GuardResult("secrets", False, output)])

    assert result.summary == '  ✗ secrets — .env:3: [hardcoded API key] value="***"'


def test_tier1_mutable_defaults_are_isolated_and_instance_is_frozen():
    first = Tier1Result(passed=True)
    second = Tier1Result(passed=True)

    first.results.append(GuardResult("lint", True))
    first.extra["key"] = "value"
    first.warnings.append("warning")

    assert second.results == []
    assert second.extra == {}
    assert second.warnings == []
    with pytest.raises(FrozenInstanceError):
        setattr(first, "passed", False)
