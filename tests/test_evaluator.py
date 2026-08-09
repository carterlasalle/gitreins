"""
Unit tests for engine/evaluator.py — agentic LLM loop with tools and dedup.
axiom:trace work_item=GR-001 spec=specs/03-Agentic-Evaluator.md plan=.memory-bank/work-items/GR-001/plan.yaml
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from engine.evaluator import (
    AgenticEvaluator,
    Verdict,
    VerdictItem,
)
from engine.llm import LLMResponse, ToolCall


# ── Phase 1-4-1: AgenticEvaluator tools, dedup, verdict parsing ──────────────


class TestReadFile:
    """Test _tool_read_file — path safety, error handling — step-1-4-1-1."""

    def test_read_existing_file(self, evaluator, tmp_workdir):
        """read_file of existing file returns content."""
        path = os.path.join(tmp_workdir, "hello.txt")
        with open(path, "w") as f:
            f.write("hello world\n")
        result = evaluator._tool_read_file("hello.txt")
        assert "content" in result
        assert result["path"] == "hello.txt"
        assert result["total_lines"] == 1

    def test_read_nonexistent_file(self, evaluator):
        """read_file of nonexistent file returns error: File not found."""
        result = evaluator._tool_read_file("nonexistent.txt")
        assert "error" in result
        assert "File not found" in result["error"]

    def test_read_path_traversal_outside_workdir(self, evaluator):
        """read_file with path containing '../' outside workdir returns error."""
        result = evaluator._tool_read_file("../etc/passwd")
        assert "error" in result
        assert "Path outside working tree" in result["error"]

    def test_read_directory_returns_error(self, evaluator, tmp_workdir):
        """read_file of a directory returns error: Path is a directory."""
        subdir = os.path.join(tmp_workdir, "subdir")
        os.makedirs(subdir)
        result = evaluator._tool_read_file("subdir")
        assert "error" in result
        assert "Path is a directory" in result["error"]

    def test_read_file_with_offset(self, evaluator, tmp_workdir):
        """read_file with offset reads from given line."""
        path = os.path.join(tmp_workdir, "multiline.txt")
        with open(path, "w") as f:
            f.write("line1\nline2\nline3\nline4\n")
        result = evaluator._tool_read_file("multiline.txt", offset=2, limit=2)
        assert result["total_lines"] == 4
        content = result["content"]
        assert "line2" in content
        assert "line3" in content
        # line1 should not be present
        assert "line1" not in content

    def test_read_file_offset_exceeds_length(self, evaluator, tmp_workdir):
        """read_file with offset exceeding file length returns error."""
        path = os.path.join(tmp_workdir, "short.txt")
        with open(path, "w") as f:
            f.write("one\n")
        result = evaluator._tool_read_file("short.txt", offset=10)
        assert "error" in result

    def test_read_file_byte_mode(self, evaluator, tmp_workdir):
        """Byte mode reads raw bytes from the file."""
        path = os.path.join(tmp_workdir, "data.txt")
        with open(path, "wb") as f:
            f.write(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        result = evaluator._tool_read_file("data.txt", mode="bytes", byte_offset=5, byte_limit=5)
        assert result["mode"] == "bytes"
        assert result["shown_bytes"] == 5
        assert result["byte_offset_start"] == 5
        assert result["content"] == "FGHIJ"
        assert result["total_bytes"] == 26
        assert result["has_more"] is True

    def test_read_file_byte_mode_full_file(self, evaluator, tmp_workdir):
        """Byte mode without byte_limit reads to end of file."""
        path = os.path.join(tmp_workdir, "data.txt")
        with open(path, "wb") as f:
            f.write(b"HELLO WORLD")
        result = evaluator._tool_read_file("data.txt", mode="bytes")
        assert result["mode"] == "bytes"
        assert result["shown_bytes"] == 11
        assert result["content"] == "HELLO WORLD"
        assert result["total_bytes"] == 11
        assert result["has_more"] is False

    def test_read_file_byte_offset_exceeds_size(self, evaluator, tmp_workdir):
        """Byte offset exceeding file size returns error."""
        path = os.path.join(tmp_workdir, "small.txt")
        with open(path, "wb") as f:
            f.write(b"xy")
        result = evaluator._tool_read_file("small.txt", mode="bytes", byte_offset=100)
        assert "error" in result
        assert "exceeds file size" in result["error"]
        assert result["total_bytes"] == 2

    def test_read_file_default_mode_is_lines(self, evaluator, tmp_workdir):
        """Default mode is 'lines' — offset/limit still work as before."""
        path = os.path.join(tmp_workdir, "lines.txt")
        with open(path, "w") as f:
            f.write("line1\nline2\nline3\nline4\nline5\n")
        result = evaluator._tool_read_file("lines.txt", offset=2, limit=2)
        assert result["mode"] == "lines"
        assert result["content"] == "line2\nline3\n"
        assert result["total_lines"] == 5
        assert result["shown_lines"] == 2

    def test_read_file_byte_mode_binary_escaping(self, evaluator, tmp_workdir):
        """Byte mode with binary content uses replacement chars, doesn't crash."""
        path = os.path.join(tmp_workdir, "binary.bin")
        with open(path, "wb") as f:
            f.write(bytes(range(256)))
        result = evaluator._tool_read_file("binary.bin", mode="bytes", byte_offset=0, byte_limit=10)
        assert result["mode"] == "bytes"
        assert result["shown_bytes"] == 10
        assert result["total_bytes"] == 256
        # Content decodes with replacement chars for non-UTF-8 bytes
        assert "content" in result


class TestRunCommand:
    """Test _tool_run_command — execution, output, timeout — step-1-4-1-2."""

    def test_run_echo_hello(self, evaluator):
        """run_command('echo hello') → exit_code=0, output contains 'hello'."""
        result = evaluator._tool_run_command("echo hello")
        assert result["exit_code"] == 0
        assert "hello" in result["output"]
        assert result["cmd"] == "echo hello"

    def test_run_false_command(self, evaluator):
        """run_command('false') → exit_code=1, output captured."""
        result = evaluator._tool_run_command("false")
        assert result["exit_code"] == 1

    def test_run_command_timeout(self, evaluator, llm_client, tmp_workdir):
        """run_command with short timeout → timeout error in <5s."""
        fast_eval = AgenticEvaluator(llm_client, tmp_workdir, max_iterations=5, command_timeout=2)
        result = fast_eval._tool_run_command("sleep 5")
        assert "error" in result
        assert "timed out" in result["error"]

    def test_output_truncated_at_4000(self, evaluator):
        """Output is truncated at 4000 characters."""
        # Generate > 4000 chars of output
        result = evaluator._tool_run_command("python3 -c 'print(\"x\" * 5000)'")
        output_len = len(result["output"])
        assert output_len <= 4100  # Allow some margin for truncation message
        assert "truncated" in result["output"]

    def test_run_command_strips_budget_and_llm_cred_env(self, evaluator, monkeypatch):
        """run_command must not inherit GITREINS_MAX_* budget or LLM cred vars.

        Proven 2026-08-09 R2-16: judge caps leaked into the pytest subprocess
        and broke EvalCap/config-priority tests (assert 2000000 == 1000000).
        INFRA-LLM-ENV-001: GITREINS_LLM_* / OPENROUTER_API_KEY leak breaks
        test_llm.py env-priority tests (api_key == '' asserted, real key
        found).
        """
        from unittest.mock import patch as _patch

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return SimpleNamespace(stdout="ok", stderr="", returncode=0)

        monkeypatch.setenv("GITREINS_MAX_OUTPUT_TOKENS", "2M")
        monkeypatch.setenv("GITREINS_MAX_ITERATIONS", "400")
        monkeypatch.setenv("GITREINS_LLM_API_KEY", "sk-keep")
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
        monkeypatch.setenv("KIMI_API_KEY", "kimi-key")
        monkeypatch.setenv("GROQ_API_KEY", "groq-key")
        with _patch("engine.evaluator.subprocess.run", side_effect=fake_run):
            result = evaluator._tool_run_command("pytest")
        assert result["exit_code"] == 0
        assert "GITREINS_MAX_OUTPUT_TOKENS" not in captured["env"]
        assert "GITREINS_MAX_ITERATIONS" not in captured["env"]
        assert "GITREINS_LLM_API_KEY" not in captured["env"]
        assert "OPENROUTER_API_KEY" not in captured["env"]
        assert "KIMI_API_KEY" not in captured["env"]
        assert "GROQ_API_KEY" not in captured["env"]


class TestSearchPattern:
    """Test _tool_search_pattern — regex matching, dir skipping, result capping — step-1-4-1-3."""

    def test_search_finds_todo(self, evaluator, tmp_workdir):
        """search_pattern finds TODO comments in working files."""
        path = os.path.join(tmp_workdir, "code.py")
        with open(path, "w") as f:
            f.write("# TODO: fix this\nprint('hello')\n# TODO: also this\n")
        result = evaluator._tool_search_pattern("TODO")
        assert result["count"] >= 2

    def test_search_with_file_glob(self, evaluator, tmp_workdir):
        """search_pattern with file_glob='*.py' only searches Python files."""
        py_path = os.path.join(tmp_workdir, "code.py")
        txt_path = os.path.join(tmp_workdir, "notes.txt")
        with open(py_path, "w") as f:
            f.write("# TODO in py\n")
        with open(txt_path, "w") as f:
            f.write("TODO in txt\n")
        result = evaluator._tool_search_pattern("TODO", file_glob="*.py")
        assert result["count"] == 1
        assert all("code.py" in m for m in result["matches"])

    def test_invalid_regex_returns_error(self, evaluator):
        """Invalid regex pattern returns error."""
        result = evaluator._tool_search_pattern("[invalid(regex")
        assert "error" in result
        assert "Invalid regex" in result["error"]

    def test_search_skips_dot_dirs(self, evaluator, tmp_workdir):
        """search_pattern does not descend into .git/ or __pycache__/."""
        # Create files in skipped dirs
        cache_dir = os.path.join(tmp_workdir, "__pycache__")
        os.makedirs(cache_dir)
        with open(os.path.join(cache_dir, "cache.py"), "w") as f:
            f.write("SECRET in cache\n")
        result = evaluator._tool_search_pattern("SECRET")
        # Should not match the cache file
        matches = result["matches"]
        cache_matches = [m for m in matches if "__pycache__" in m]
        assert len(cache_matches) == 0


class TestSandbox:
    """Test sandbox_write/sandbox_read — in-memory dict — step-1-4-1-4."""

    def test_sandbox_write_read(self, evaluator):
        """sandbox_write then sandbox_read returns content."""
        evaluator._tool_sandbox_write("key1", "value1")
        result = evaluator._tool_sandbox_read("key1")
        assert result["content"] == "value1"
        assert result["key"] == "key1"

    def test_sandbox_write_returns_written_count(self, evaluator):
        """sandbox_write returns written count."""
        result = evaluator._tool_sandbox_write("key2", "hello")
        assert result["written"] == 5

    def test_sandbox_read_nonexistent(self, evaluator):
        """sandbox_read of nonexistent key returns error."""
        result = evaluator._tool_sandbox_read("nonexistent")
        assert "error" in result
        assert "Key not found" in result["error"]

    def test_evaluate_clears_sandbox(self, evaluator, llm_client):
        """evaluate() clears sandbox at start of each call."""
        evaluator._tool_sandbox_write("persist", "data")
        # Mock LLM to immediately return verdict
        verdict_json = '{"verdict":"COMPLETE","items":[],"summary":"done"}'
        with patch.object(llm_client, "chat", return_value=LLMResponse(content=verdict_json)):
            evaluator.evaluate({"id": "t1", "title": "x", "criteria": []})
        # Sandbox should be cleared
        result = evaluator._tool_sandbox_read("persist")
        assert "error" in result


class TestDeduplication:
    """Test dedup tracking — step-1-4-1-5."""

    def test_repeated_read_file_is_duplicate(self, evaluator, tmp_workdir):
        """Reading the same file twice marks second call as duplicate."""
        path = os.path.join(tmp_workdir, "dup_test.txt")
        with open(path, "w") as f:
            f.write("content")
        tc = ToolCall(id="c1", name="read_file", arguments={"path": "dup_test.txt"})
        result1, was_dup1 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup1 is False
        result2, was_dup2 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup2 is True
        # was_dup flag is set — _dedup_warning is added by evaluate() loop, not here
        assert "path" in result2
        assert result2["path"] == "dup_test.txt"

    def test_repeated_run_command_is_duplicate(self, evaluator):
        """Running the same command twice marks second as duplicate."""
        tc = ToolCall(id="c2", name="run_command", arguments={"cmd": "echo test"})
        _, was_dup1 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup1 is False
        _, was_dup2 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup2 is True

    def test_repeated_search_pattern_is_duplicate(self, evaluator):
        """Searching the same regex twice marks second as duplicate."""
        tc = ToolCall(id="c3", name="search_pattern", arguments={"regex": r"def test_"})
        _, was_dup1 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup1 is False
        _, was_dup2 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup2 is True

    def test_different_files_not_flagged(self, evaluator, tmp_workdir):
        """Different files are not flagged as duplicates."""
        path1 = os.path.join(tmp_workdir, "a.txt")
        path2 = os.path.join(tmp_workdir, "b.txt")
        with open(path1, "w") as f:
            f.write("a")
        with open(path2, "w") as f:
            f.write("b")
        tc1 = ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})
        tc2 = ToolCall(id="c2", name="read_file", arguments={"path": "b.txt"})
        _, was_dup1 = evaluator._execute_tool_with_dedup(tc1)
        _, was_dup2 = evaluator._execute_tool_with_dedup(tc2)
        assert was_dup1 is False
        assert was_dup2 is False


class TestVerdictParsing:
    """Test _parse_verdict — JSON parse, markdown fence, keyword fallback — step-1-4-1-6."""

    def test_valid_json_verdict(self, evaluator):
        """Valid JSON verdict parses correctly."""
        content = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"ok"}],"summary":"all good"}'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "COMPLETE"
        assert len(verdict.items) == 1
        assert verdict.items[0].criterion == "c1"
        assert verdict.items[0].status == "PASS"
        assert verdict.items[0].detail == "ok"
        assert verdict.summary == "all good"

    def test_json_in_markdown_fences(self, evaluator):
        """JSON wrapped in ```json``` fences is stripped and parsed."""
        content = '```json\n{"verdict":"INCOMPLETE","items":[{"criterion":"c1","status":"FAIL","detail":"missing"}],"summary":"nope"}\n```'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "INCOMPLETE"
        assert len(verdict.items) == 1

    def test_json_with_extra_text(self, evaluator):
        """JSON with text before/after is extracted and parsed."""
        content = 'Here is my verdict:\n{"verdict":"COMPLETE","items":[],"summary":"done"}\nHope this helps!'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "COMPLETE"

    def test_invalid_status_defaults_to_fail(self, evaluator):
        """Invalid status ('MAYBE') defaults to FAIL."""
        content = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"MAYBE","detail":"x"}],"summary":"x"}'
        verdict = evaluator._parse_verdict(content)
        assert verdict.items[0].status == "FAIL"

    def test_invalid_verdict_defaults_to_incomplete(self, evaluator):
        """Invalid verdict ('ALMOST') defaults to INCOMPLETE."""
        content = '{"verdict":"ALMOST","items":[],"summary":"almost done"}'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "INCOMPLETE"

    def test_missing_items_falls_to_keyword(self, evaluator):
        """JSON missing 'items' key falls to keyword parse."""
        content = '{"verdict":"COMPLETE","summary":"all criteria pass"}'
        verdict = evaluator._parse_verdict(content)
        # Should fall to keyword parse (which finds 'complete' → COMPLETE)
        assert verdict.verdict == "COMPLETE"

    def test_keyword_complete_detected(self, evaluator):
        """Content with 'all criteria pass' → keyword parse yields COMPLETE."""
        content = "I have verified all criteria, everything passes and is complete."
        verdict = evaluator._parse_verdict(content)
        # Keyword fallback: "all criteria" + "pass" → COMPLETE
        assert verdict.verdict == "COMPLETE"

    def test_keyword_all_criteria_pass(self, evaluator):
        """Content with 'all criteria' and 'pass' → COMPLETE."""
        content = "After reviewing, all criteria pass."
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "COMPLETE"

    def test_empty_response_is_incomplete(self, evaluator):
        """Empty response → INCOMPLETE with error summary."""
        verdict = evaluator._parse_verdict("")
        assert verdict.verdict == "INCOMPLETE"
        assert "auto-parsed" in verdict.summary


class TestMaxIterationsAndErrors:
    """Test max_iterations cap and error handling — step-1-4-1-7."""

    def test_max_iterations_reached_returns_incomplete(self, evaluator, llm_client):
        """Evaluator stops at max_iterations and returns INCOMPLETE with actionable error."""
        # Always return tool calls (never a verdict) — exhausts the cap
        calls = []
        for i in range(10):
            tc = ToolCall(id=f"tc{i}", name="read_file", arguments={"path": f"f{i}.txt"})
            calls.append(LLMResponse(content="working", tool_calls=[tc]))
        mock_chat = MagicMock(side_effect=calls)
        with patch.object(llm_client, "chat", mock_chat):
            from engine.evaluator import AgenticEvaluator

            fast_eval = AgenticEvaluator(llm_client, evaluator.workdir, max_iterations=2)
            verdict = fast_eval.evaluate({"id": "loop", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "INCOMPLETE"
        assert "Cap exceeded" in verdict.summary
        assert "Increase max_iterations" in verdict.summary

    def test_max_iterations_error_message_actionable(self, evaluator, llm_client):
        """Error message on cap tells user exactly how to fix it."""
        calls = [
            LLMResponse(
                content="working",
                tool_calls=[ToolCall(id="t0", name="read_file", arguments={"path": "x.txt"})],
            )
            for _ in range(5)
        ]
        mock_chat = MagicMock(side_effect=calls)
        with patch.object(llm_client, "chat", mock_chat):
            from engine.evaluator import AgenticEvaluator

            fast_eval = AgenticEvaluator(llm_client, evaluator.workdir, max_iterations=1)
            verdict = fast_eval.evaluate({"id": "t", "title": "x", "criteria": ["c1"]})
        assert "max_iterations" in verdict.summary
        assert "split criteria" in verdict.summary
        assert str(fast_eval.max_iterations) in verdict.summary

    def test_default_max_iterations_is_100(self, llm_client, tmp_workdir):
        """Default max_iterations is 100 (was 15 — too low for complex criteria)."""
        evaluator = AgenticEvaluator(llm_client, tmp_workdir)
        assert evaluator.max_iterations == 100

    def test_llm_exception_returns_incomplete(self, evaluator, llm_client):
        """LLM exception → INCOMPLETE with error summary."""
        with patch.object(llm_client, "chat", side_effect=RuntimeError("LLM crashed")):
            verdict = evaluator.evaluate({"id": "err", "title": "x", "criteria": []})
        assert verdict.verdict == "INCOMPLETE"
        assert "LLM call failed" in verdict.summary or "LLM crashed" in verdict.summary

    def test_custom_max_iterations(self, evaluator):
        """Evaluator respects custom max_iterations."""
        assert evaluator.max_iterations == 5
        custom = AgenticEvaluator(evaluator.llm, evaluator.workdir, max_iterations=20)
        assert custom.max_iterations == 20


class TestEvaluatorTools:
    """Test additional tool implementations."""

    def test_unknown_tool_returns_error(self, evaluator):
        """Unknown tool name returns error."""
        tc = ToolCall(id="x", name="nonexistent_tool", arguments={})
        result = evaluator._execute_tool(tc)
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_read_diff_basic(self, evaluator):
        """read_diff tool returns staged/unstaged info."""
        result = evaluator._tool_read_diff()
        assert "staged" in result
        assert "unstaged" in result

    def test_get_task_item_found(self, evaluator):
        """get_task_item returns the task dict when found."""
        task = {"id": "t1", "title": "Test", "criteria": ["c1"]}
        evaluator._task_index["t1"] = task
        result = evaluator._tool_get_task_item("t1")
        assert result["id"] == "t1"

    def test_get_task_item_not_found(self, evaluator):
        """get_task_item returns error when task not found."""
        result = evaluator._tool_get_task_item("nonexistent")
        assert "error" in result
        assert "Task not found" in result["error"]


class TestEvaluatorEvaluate:
    """Test the full evaluate loop."""

    def test_evaluate_with_empty_criteria_returns_complete(self, evaluator, llm_client):
        """Task with no criteria — LLM should return COMPLETE immediately."""
        verdict_json = '{"verdict":"COMPLETE","items":[],"summary":"no criteria to check"}'
        with patch.object(llm_client, "chat", return_value=LLMResponse(content=verdict_json)):
            verdict = evaluator.evaluate({"id": "empty", "title": "x", "criteria": []})
        assert verdict.verdict == "COMPLETE"

    def test_verdict_item_dataclass(self):
        """VerdictItem and Verdict dataclasses work correctly."""
        item = VerdictItem(criterion="c1", status="PASS", detail="verified")
        assert item.criterion == "c1"
        assert item.status == "PASS"
        verdict = Verdict(verdict="COMPLETE", items=[item], summary="done")
        assert verdict.verdict == "COMPLETE"
        assert len(verdict.items) == 1


class TestExtendedEvaluator:
    """Extended edge case coverage for AgenticEvaluator."""

    def test_search_truncated_at_200(self, evaluator, tmp_workdir):
        """search_pattern truncates results at 200 matches."""
        path = os.path.join(tmp_workdir, "big.py")
        with open(path, "w") as f:
            for _ in range(300):
                f.write("MATCH_ME\n")
        result = evaluator._tool_search_pattern("MATCH_ME")
        # rg/grep paths cap at 200; Python path adds truncation marker
        assert len(result["matches"]) >= 200
        assert len(result["matches"]) <= 201

    def test_sandbox_read_truncated_at_4000(self, evaluator):
        """sandbox_read truncates content at 4000 chars."""
        evaluator._sandbox["big"] = "x" * 5000
        result = evaluator._tool_sandbox_read("big")
        assert len(result["content"]) <= 4100

    def test_read_file_truncation_large_file(self, evaluator, tmp_workdir):
        """read_file truncates very large files (>12000 chars) to first 400 lines."""
        path = os.path.join(tmp_workdir, "huge.py")
        with open(path, "w") as f:
            for i in range(1000):
                f.write(f"line {i}: xyz\n")
        result = evaluator._tool_read_file("huge.py")
        assert result["total_lines"] == 1000
        assert result["total_chars"] > 12000
        content = result["content"]
        assert "... [showing first 400" in content

    def test_execute_tool_wraps_exception(self, evaluator):
        """_execute_tool wraps exceptions in error dict."""
        tc = ToolCall(id="x", name="read_file", arguments={})
        result = evaluator._execute_tool(tc)
        assert "error" in result

    def test_dedup_warning_in_evaluate_loop(self, evaluator, llm_client, tmp_workdir):
        """_dedup_warning is added to tool result in evaluate() loop."""
        path = os.path.join(tmp_workdir, "dedup_loop.txt")
        with open(path, "w") as f:
            f.write("content")

        # LLM returns same tool call twice, then a verdict
        tc1 = ToolCall(id="tc1", name="read_file", arguments={"path": "dedup_loop.txt"})
        tc2 = ToolCall(id="tc2", name="read_file", arguments={"path": "dedup_loop.txt"})
        verdict_json = '{"verdict":"COMPLETE","items":[],"summary":"done"}'
        mock = MagicMock()
        mock.side_effect = [
            LLMResponse(content="checking", tool_calls=[tc1]),
            LLMResponse(content="checking again", tool_calls=[tc2]),
            LLMResponse(content=verdict_json),
        ]
        with patch.object(llm_client, "chat", mock):
            evaluator.max_iterations = 5
            verdict = evaluator.evaluate({"id": "dt", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "COMPLETE"

    def test_evaluate_with_tool_calls_then_verdict(self, evaluator, llm_client, tmp_workdir):
        """evaluate() handles tool calls followed by a verdict."""
        path = os.path.join(tmp_workdir, "eval_me.py")
        with open(path, "w") as f:
            f.write("def foo(): pass\n")
        tc = ToolCall(id="tc1", name="read_file", arguments={"path": "eval_me.py"})
        verdict_json = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"verified"}],"summary":"pass"}'
        mock = MagicMock()
        mock.side_effect = [
            LLMResponse(content="reading", tool_calls=[tc]),
            LLMResponse(content=verdict_json),
        ]
        with patch.object(llm_client, "chat", mock):
            evaluator.max_iterations = 5
            verdict = evaluator.evaluate({"id": "et", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "COMPLETE"
        assert len(verdict.items) == 1

    def test_verdict_keyword_complete_detected(self, evaluator):
        """Content with 'complete' keyword and PASS items yields COMPLETE."""
        content = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"ok"}],"summary":"all pass"}'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "COMPLETE"

    def test_verdict_missing_items_falls_to_keyword(self, evaluator):
        """JSON with verdict but missing items falls back to keyword parse."""
        content = "The task is complete. All criteria pass."
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "COMPLETE"

    def test_search_all_files_with_glob(self, evaluator, tmp_workdir):
        """search_pattern with file_glob='*' searches all file types."""
        py_path = os.path.join(tmp_workdir, "a.py")
        txt_path = os.path.join(tmp_workdir, "b.txt")
        with open(py_path, "w") as f:
            f.write("MATCH_ME\n")
        with open(txt_path, "w") as f:
            f.write("MATCH_ME\n")
        result = evaluator._tool_search_pattern("MATCH_ME", file_glob="*")
        assert result["count"] >= 2

    def test_read_file_truncation_large_file_with_offset(self, evaluator, tmp_workdir):
        """read_file with explicit offset/limit does NOT auto-truncate."""
        path = os.path.join(tmp_workdir, "huge2.py")
        with open(path, "w") as f:
            for i in range(500):
                f.write(f"line {i}\n")
        result = evaluator._tool_read_file("huge2.py", offset=400, limit=50)
        assert result["total_lines"] == 500
        content = result["content"]
        assert "line 400" in content
        assert "... [showing first" not in content

    def test_evaluate_empty_response_no_tool_calls(self, evaluator, llm_client):
        """evaluate() returns INCOMPLETE when LLM returns empty content with no tool calls."""
        with patch.object(llm_client, "chat", return_value=LLMResponse(content=None)):
            verdict = evaluator.evaluate({"id": "empty", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "INCOMPLETE"
        assert "empty response" in verdict.summary.lower()

    def test_evaluate_tool_exception_returns_error(self, evaluator, llm_client):
        """evaluate() returns INCOMPLETE when a tool call raises an exception."""
        tc = ToolCall(id="bad", name="read_file", arguments={"path": "/nonexistent"})
        with patch.object(
            llm_client,
            "chat",
            side_effect=[
                LLMResponse(content="checking", tool_calls=[tc]),
                LLMResponse(content='{"verdict":"INCOMPLETE","items":[],"summary":"error"}'),
            ],
        ):
            verdict = evaluator.evaluate({"id": "err", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "INCOMPLETE"

    def test_verdict_status_fail_default(self, evaluator):
        """Missing status in verdict item defaults to FAIL."""
        content = '{"verdict":"COMPLETE","items":[{"criterion":"c1","detail":"x"}],"summary":"s"}'
        verdict = evaluator._parse_verdict(content)
        assert verdict.items[0].status == "FAIL"

    def test_verdict_has_more_field_read_file(self, evaluator, tmp_workdir):
        """read_file returns has_more=True when file is large."""
        path = os.path.join(tmp_workdir, "bigfile.py")
        with open(path, "w") as f:
            for i in range(500):
                f.write(f"line {i}: {'x' * 100}\n")
        result = evaluator._tool_read_file("bigfile.py")
        assert "has_more" in result


# ── v0.7.5: Compaction checkpoint + resume loop ──────────────────────


class TestCompaction:
    """Tests for context compaction: checkpoint, resume, sandbox survival."""

    def test_build_compacted_prompt_extracts_verified_from_sandbox(self, evaluator):
        """_build_compacted_prompt reads verified_N keys from sandbox."""
        evaluator._sandbox["verified_0"] = "PASS: tests/test_auth.py:45"
        evaluator._sandbox["verified_2"] = "FAIL: missing handler"

        task = {"id": "t1", "title": "Test", "criteria": ["c0", "c1", "c2"]}
        prompt = evaluator._build_compacted_prompt(task, "", 3)

        assert "Already verified (2/3)" in prompt
        assert "c0" in prompt
        assert "tests/test_auth.py:45" in prompt
        assert "c2" in prompt  # criteria text
        assert "Still to verify (1/3)" in prompt
        assert "c1" in prompt  # remaining

    def test_build_compacted_prompt_all_verified(self, evaluator):
        """All criteria verified → no 'still to verify' section."""
        evaluator._sandbox["verified_0"] = "PASS"
        evaluator._sandbox["verified_1"] = "PASS"
        task = {"id": "t1", "title": "Test", "criteria": ["c0", "c1"]}
        prompt = evaluator._build_compacted_prompt(task, "", 2)
        assert "Already verified (2/2)" in prompt
        assert "Still to verify" not in prompt

    def test_build_compacted_prompt_none_verified(self, evaluator):
        """No sandbox keys → all criteria listed as remaining."""
        task = {"id": "t1", "title": "Test", "criteria": ["c0", "c1", "c2"]}
        prompt = evaluator._build_compacted_prompt(task, "", 3)
        assert "Already verified" not in prompt
        assert "Still to verify (3/3)" in prompt

    def test_compact_context_rebuilds_clean_messages(self, evaluator):
        """_compact_context returns fresh messages with only system + compacted prompt."""
        evaluator._sandbox["verified_0"] = "PASS"
        task = {"id": "t1", "title": "Test", "criteria": ["c0", "c1"]}
        old_messages = [{"role": "system", "content": "old"}, {"role": "user", "content": "old"}]
        new_msgs, count = evaluator._compact_context(old_messages, task, "", 2, 1)
        assert count == 2  # was compaction #1, now #2
        assert len(new_msgs) == 2
        assert new_msgs[0]["role"] == "system"
        assert "EVALUATION PROGRESS" in new_msgs[1]["content"]
        assert "c0" in new_msgs[1]["content"]

    def test_compact_context_increments_count(self, evaluator):
        """Each compaction increments the counter."""
        task = {"id": "t1", "title": "Test", "criteria": ["c0"]}
        _, count1 = evaluator._compact_context([], task, "", 1, 0)
        _, count2 = evaluator._compact_context([], task, "", 1, 3)
        assert count1 == 1
        assert count2 == 4

    def test_compaction_triggered_on_http_400(self, evaluator, llm_client):
        """HTTP 400 with 'context' keyword triggers compaction, not immediate failure."""
        import requests

        # Build a fake HTTPError with status 400
        fake_response = MagicMock()
        fake_response.status_code = 400
        http_err = requests.HTTPError("context length exceeded", response=fake_response)

        # First call: HTTP 400 → compact
        # Second call: verdict (after compaction, fresh context)
        evaluator._sandbox["verified_0"] = "PASS"
        with patch.object(
            llm_client,
            "chat",
            side_effect=[
                http_err,
                LLMResponse(
                    content='{"verdict":"COMPLETE","items":[{"criterion":"c0","status":"PASS","detail":"x"}],"summary":"ok"}'
                ),
            ],
        ):
            verdict = evaluator.evaluate({"id": "t1", "title": "Test", "criteria": ["c0"]})
        assert verdict.verdict == "COMPLETE"

    def test_compaction_not_triggered_on_other_error(self, evaluator, llm_client):
        """Non-context errors (like ValueError) return INCOMPLETE immediately."""
        with patch.object(llm_client, "chat", side_effect=ValueError("some other error")):
            verdict = evaluator.evaluate({"id": "t1", "title": "Test", "criteria": ["c0"]})
        assert verdict.verdict == "INCOMPLETE"
        assert "other error" in verdict.summary.lower()

    def test_proactive_compaction_at_90pct_threshold(self, evaluator, llm_client):
        """When cumulative prompt tokens exceed 90% of limit, compaction triggers by default."""
        evaluator.eval_cap.max_input_tokens = 1000  # 1000 token limit

        call_count = [0]

        def fake_chat(messages, tools=None, max_tokens=None):
            call_count[0] += 1
            if call_count[0] <= 2:
                # Build up context: 950 prompt tokens > 900 (90% of 1000)
                tc = ToolCall(
                    id=f"tc{call_count[0]}",
                    name="read_file",
                    arguments={"path": f"f{call_count[0]}.py"},
                )
                return LLMResponse(
                    content="checking",
                    tool_calls=[tc],
                    usage=MagicMock(
                        prompt_tokens=950,
                        completion_tokens=10,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        total_tokens=910,
                    ),
                )
            else:
                # After compaction: deliver verdict
                return LLMResponse(
                    content='{"verdict":"COMPLETE","items":[{"criterion":"c0","status":"PASS","detail":"ok"}],"summary":"done"}',
                )

        with patch.object(llm_client, "chat", side_effect=fake_chat):
            with patch.object(
                evaluator, "_tool_read_file", return_value={"content": "test", "total_lines": 1}
            ):
                verdict = evaluator.evaluate({"id": "t1", "title": "Test", "criteria": ["c0"]})
        assert verdict.verdict == "COMPLETE"

    def test_max_compactions_limit(self, evaluator, llm_client):
        """After MAX_COMPACTIONS (3), HTTP 400 returns INCOMPLETE."""
        import requests

        fake_response = MagicMock()
        fake_response.status_code = 400
        http_err = requests.HTTPError("context length exceeded", response=fake_response)

        with patch.object(llm_client, "chat", side_effect=http_err):
            verdict = evaluator.evaluate({"id": "t1", "title": "Test", "criteria": ["c0"]})
        assert verdict.verdict == "INCOMPLETE"
        # Should have failed after 3 compaction attempts
        assert "LLM call failed" in verdict.summary

    def test_compaction_threshold_config_override(self, evaluator, llm_client, tmp_workdir):
        """When config sets compaction_threshold to 0.50, compaction triggers at 50%."""
        import yaml

        cdir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "config.yaml"), "w") as f:
            yaml.dump({"evaluator": {"compaction_threshold": 0.50}}, f)

        evaluator.eval_cap.max_input_tokens = 1000  # 1000 token limit

        call_count = [0]

        def fake_chat(messages, tools=None, max_tokens=None):
            call_count[0] += 1
            if call_count[0] <= 2:
                # 600 prompt tokens > 500 (50% of 1000) — should trigger compaction
                tc = ToolCall(
                    id=f"tc{call_count[0]}",
                    name="read_file",
                    arguments={"path": f"f{call_count[0]}.py"},
                )
                return LLMResponse(
                    content="checking",
                    tool_calls=[tc],
                    usage=MagicMock(
                        prompt_tokens=600,
                        completion_tokens=10,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        total_tokens=610,
                    ),
                )
            else:
                return LLMResponse(
                    content='{"verdict":"COMPLETE","items":[{"criterion":"c0","status":"PASS","detail":"ok"}],"summary":"done"}',
                )

        with patch.object(llm_client, "chat", side_effect=fake_chat):
            with patch.object(
                evaluator, "_tool_read_file", return_value={"content": "test", "total_lines": 1}
            ):
                verdict = evaluator.evaluate({"id": "t1", "title": "Test", "criteria": ["c0"]})
        assert verdict.verdict == "COMPLETE"

    def test_compaction_threshold_not_triggered_below(self, evaluator, llm_client, tmp_workdir):
        """When prompt tokens are below the configured threshold, no compaction occurs."""
        import yaml

        cdir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "config.yaml"), "w") as f:
            yaml.dump({"evaluator": {"compaction_threshold": 0.50}}, f)

        evaluator.eval_cap.max_input_tokens = 10000  # large limit

        call_count = [0]

        def fake_chat(messages, tools=None, max_tokens=None):
            call_count[0] += 1
            if call_count[0] <= 2:
                # 400 prompt tokens < 5000 (50% of 10000) — should NOT trigger compaction
                tc = ToolCall(
                    id=f"tc{call_count[0]}",
                    name="read_file",
                    arguments={"path": f"f{call_count[0]}.py"},
                )
                return LLMResponse(
                    content="checking",
                    tool_calls=[tc],
                    usage=MagicMock(
                        prompt_tokens=400,
                        completion_tokens=10,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        total_tokens=410,
                    ),
                )
            else:
                return LLMResponse(
                    content='{"verdict":"COMPLETE","items":[{"criterion":"c0","status":"PASS","detail":"ok"}],"summary":"done"}',
                )

        with patch.object(llm_client, "chat", side_effect=fake_chat):
            with patch.object(
                evaluator, "_tool_read_file", return_value={"content": "test", "total_lines": 1}
            ):
                verdict = evaluator.evaluate({"id": "t1", "title": "Test", "criteria": ["c0"]})
        assert verdict.verdict == "COMPLETE"
        # Should have completed without compaction — all 3 calls direct
        assert call_count[0] == 3

    def test_code_context_budget_config_override(self, evaluator, llm_client, tmp_workdir):
        """When config sets code_context_budget to 0.20, code context is capped at 20%."""
        import yaml

        cdir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "config.yaml"), "w") as f:
            yaml.dump({"evaluator": {"code_context_budget": 0.20}}, f)

        evaluator.eval_cap.max_input_tokens = 1000  # budget = 200 tokens

        # Build a very large code context — ~3000 chars (~1000 tokens)
        large_ctx = "x" * 3000

        with patch.object(evaluator, "_build_code_context", return_value=large_ctx):
            captured_prompt = []

            def capture_chat(messages, tools=None, max_tokens=None, temperature=0.1):
                captured_prompt.append(messages[1]["content"] if len(messages) > 1 else "")
                return LLMResponse(content='{"verdict":"COMPLETE","items":[],"summary":"ok"}')

            with patch.object(llm_client, "chat", capture_chat):
                evaluator.evaluate({"id": "t", "title": "ct", "criteria": ["x"]})

        assert captured_prompt, "chat() was never called"
        assert "[code context truncated" in captured_prompt[0], (
            f"Expected code context truncation at 20% of 1000=200 tokens (~600 chars), "
            f"but got no truncation. Prompt length: {len(captured_prompt[0])} chars"
        )

    def test_code_context_budget_default_70pct(self, evaluator, llm_client):
        """Without config override, code context budget defaults to 70%."""
        evaluator.eval_cap.max_input_tokens = 1000  # budget = 300 tokens

        large_ctx = "x" * 3000  # ~1000 tokens

        with patch.object(evaluator, "_build_code_context", return_value=large_ctx):
            captured_prompt = []

            def capture_chat(messages, tools=None, max_tokens=None, temperature=0.1):
                captured_prompt.append(messages[1]["content"] if len(messages) > 1 else "")
                return LLMResponse(content='{"verdict":"COMPLETE","items":[],"summary":"ok"}')

            with patch.object(llm_client, "chat", capture_chat):
                evaluator.evaluate({"id": "t", "title": "ct", "criteria": ["x"]})

        assert captured_prompt, "chat() was never called"
        assert "[code context truncated" in captured_prompt[0], (
            f"Expected code context truncation at 70% of 1000=700 tokens (~2100 chars), "
            f"but got no truncation. Prompt length: {len(captured_prompt[0])} chars"
        )

    def test_estimate_context_uses_llm_tokens(self, evaluator):
        """_estimate_context_tokens uses cumulative_prompt_tok when provided."""
        messages = [{"role": "user", "content": "hello world" * 100}]
        est = evaluator._estimate_context_tokens(messages, cumulative_prompt_tok=5000)
        assert est == 5000

    def test_estimate_context_falls_back_to_chars(self, evaluator):
        """_estimate_context_tokens falls back to char/3.5 heuristic."""
        messages = [{"role": "user", "content": "hello world" * 100}]
        est = evaluator._estimate_context_tokens(messages, cumulative_prompt_tok=0)
        assert est > 100  # Rough estimate should be reasonable


# ── v0.7.4: Code context pre-loading ─────────────────────────────────


class TestCodeContextPreloading:
    """Tests for _build_code_context: full mode vs diff mode."""

    def test_diff_mode_returns_git_diff(self, evaluator, tmp_workdir):
        """In diff mode, _build_code_context returns git diff hunks."""
        import subprocess

        os.chdir(tmp_workdir)
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_workdir)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_workdir)

        # Create and commit a file, then modify it
        with open(os.path.join(tmp_workdir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        subprocess.run(["git", "add", "main.py"], cwd=tmp_workdir)
        subprocess.run(
            ["git", "commit", "-m", "initial", "--no-verify"],
            cwd=tmp_workdir,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "t@t.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "t@t.com",
            },
        )

        with open(os.path.join(tmp_workdir, "main.py"), "w") as f:
            f.write("print('hello world')\n")

        config = {"guards": {"test_mode": "diff"}}
        ctx = evaluator._build_code_context(config)
        assert "CHANGED CODE (DIFF)" in ctx
        assert "main.py" in ctx  # Should reference the file

    def test_full_mode_returns_file_contents(self, evaluator, tmp_workdir):
        """In full mode, _build_code_context returns full changed file content."""
        import subprocess

        os.chdir(tmp_workdir)
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_workdir)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_workdir)

        with open(os.path.join(tmp_workdir, "main.py"), "w") as f:
            f.write("def foo():\n    return 42\n")
        subprocess.run(["git", "add", "main.py"], cwd=tmp_workdir)
        subprocess.run(
            ["git", "commit", "-m", "initial", "--no-verify"],
            cwd=tmp_workdir,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "t@t.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "t@t.com",
            },
        )

        with open(os.path.join(tmp_workdir, "main.py"), "w") as f:
            f.write("def foo():\n    return 99\n")

        config = {"guards": {"test_mode": "full"}}
        ctx = evaluator._build_code_context(config)
        assert "CHANGED FILES (FULL)" in ctx
        assert "def foo():" in ctx
        assert "return 99" in ctx
        assert "main.py" in ctx

    def test_no_changes_returns_empty(self, evaluator, tmp_workdir):
        """When nothing changed, _build_code_context returns empty string."""
        import subprocess

        os.chdir(tmp_workdir)
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir)
        config = {"guards": {"test_mode": "full"}}
        ctx = evaluator._build_code_context(config)
        assert ctx == ""

    def _init_repo_with_commit(self, tmp_workdir, files: dict[str, str]):
        """Helper: git init + commit the given {path: content} files."""
        import subprocess

        os.chdir(tmp_workdir)
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_workdir)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_workdir)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }
        for path, content in files.items():
            full = os.path.join(tmp_workdir, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(content)
        subprocess.run(["git", "add", "-A"], cwd=tmp_workdir)
        subprocess.run(
            ["git", "commit", "-m", "initial", "--no-verify"],
            cwd=tmp_workdir,
            env=env,
        )

    def _write(self, tmp_workdir, path: str, content: str):
        full = os.path.join(tmp_workdir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    def test_data_files_excluded_from_context(self, evaluator, tmp_workdir):
        """Regression (JUDGE-CONTEXT-001): tracked board JSONL files must not
        appear in the judge code context — neither as full-file sections nor
        as diff hunks."""
        self._init_repo_with_commit(
            tmp_workdir,
            {
                "main.py": "def foo():\n    return 42\n",
                ".coding-hermes/board/events.jsonl": '{"event": "init", "pad": "'
                + "x" * 2000
                + '"}\n',
            },
        )
        # Modify both files (unstaged) — the board file changes every tick
        self._write(tmp_workdir, "main.py", "def foo():\n    return 99\n")
        self._write(
            tmp_workdir,
            ".coding-hermes/board/events.jsonl",
            '{"event": "tick-SECRET-marker", "pad": "' + "y" * 2000 + '"}\n',
        )

        config = {"guards": {"test_mode": "full"}}
        ctx = evaluator._build_code_context(config)

        # Code file present
        assert "CHANGED FILES (FULL)" in ctx
        assert "main.py" in ctx
        assert "return 99" in ctx
        # Board data file absent — path and content must not leak anywhere
        assert "events.jsonl" not in ctx
        assert ".coding-hermes" not in ctx
        assert "tick-SECRET-marker" not in ctx

    def test_data_files_excluded_in_diff_mode(self, evaluator, tmp_workdir):
        """Diff mode also drops data-file hunks (JUDGE-CONTEXT-001)."""
        self._init_repo_with_commit(
            tmp_workdir,
            {
                "main.py": "print('a')\n",
                "tasks.jsonl": '{"task": "one", "pad": "' + "z" * 1500 + '"}\n',
            },
        )
        self._write(tmp_workdir, "main.py", "print('b')\n")
        self._write(
            tmp_workdir,
            "tasks.jsonl",
            '{"task": "two-SECRET-marker", "pad": "' + "z" * 1500 + '"}\n',
        )

        config = {"guards": {"test_mode": "diff"}}
        ctx = evaluator._build_code_context(config)

        assert "CHANGED CODE (DIFF)" in ctx
        assert "main.py" in ctx
        assert "tasks.jsonl" not in ctx
        assert "two-SECRET-marker" not in ctx

    def test_long_line_data_file_gets_preview_only(self, evaluator, tmp_workdir):
        """Files with >500 chars/line average are capped to a 5-line preview
        in FULL mode (JUDGE-CONTEXT-001)."""
        lines = [f"line-{i}-" + "x" * 1000 for i in range(10)]
        self._init_repo_with_commit(tmp_workdir, {"metrics.txt": "\n".join(lines[:5]) + "\n"})
        self._write(tmp_workdir, "metrics.txt", "\n".join(lines) + "\n")

        config = {"guards": {"test_mode": "full"}}
        ctx = evaluator._build_code_context(config)

        assert "metrics.txt" in ctx
        assert "data-like" in ctx  # preview marker, not full inclusion
        assert "5-line preview" in ctx
        # The full-file section holds only the first 5 lines (the trailing
        # diff summary may still show the other added lines — that's the
        # compact hunk view, not the file content).
        section = ctx.split("### metrics.txt")[1].split("```")[1]
        assert "line-0-" in section
        assert "line-4-" in section
        assert "line-5-" not in section
        assert "line-9-" not in section

    def test_normal_code_file_not_previewed(self, evaluator, tmp_workdir):
        """A normal code file (short lines) is NOT subject to the data-file
        preview cap even when it exceeds 200 lines."""
        body = "\n".join(f"# comment {i}" for i in range(250)) + "\n"
        self._init_repo_with_commit(tmp_workdir, {"big.py": "# v1\n"})
        self._write(tmp_workdir, "big.py", body)

        config = {"guards": {"test_mode": "full"}}
        ctx = evaluator._build_code_context(config)

        assert "data-like" not in ctx
        assert "truncated at 200 lines" in ctx  # normal 200-line cap applies

    def test_max_output_tokens_passed_to_llm(self, evaluator, llm_client):
        """Per-request max_tokens is always capped at 16384 — session budget is separate."""
        from engine.eval_cap import EvalCap

        evaluator.eval_cap = EvalCap(
            max_iterations=10,
            max_output_tokens=50_000,
        )

        captured_tokens = []

        def capture_chat(messages, max_tokens=131072, tools=None, temperature=0.1):
            captured_tokens.append(max_tokens)
            return LLMResponse(content='{"verdict":"COMPLETE","items":[],"summary":"ok"}')

        with patch.object(llm_client, "chat", capture_chat):
            evaluator.evaluate({"id": "t", "title": "token test", "criteria": ["x"]})

        assert captured_tokens, "chat() was never called"
        # Per-request max_tokens is always 16384 regardless of session budget
        assert captured_tokens[0] == 16384, (
            f"Expected per-request max_tokens=16384 (session budget is 50000), got {captured_tokens[0]}"
        )

    def test_max_tokens_fallback_when_nothing_configured(self, evaluator, llm_client):
        """When nothing set (unlimited), falls back to 16384 — not a stale default."""
        from engine.eval_cap import EvalCap

        evaluator.eval_cap = EvalCap(
            max_iterations=10,
            max_output_tokens=-1,  # unlimited — nothing configured
        )

        captured = []

        def capture_chat(messages, max_tokens, tools=None, temperature=0.1):
            captured.append(max_tokens)
            return LLMResponse(content='{"verdict":"COMPLETE","items":[],"summary":"ok"}')

        with patch.object(llm_client, "chat", capture_chat):
            evaluator.evaluate({"id": "t", "title": "unlimited", "criteria": ["x"]})

        assert captured, "chat() was never called"
        assert captured[0] == 16384, (
            f"Expected fallback max_tokens=16384 when unlimited, got {captured[0]}"
        )

    def test_max_tokens_value_set_not_corrupted(self, evaluator, llm_client):
        """Session budget tracked via EvalCap, per-request max_tokens always 16384."""
        from engine.eval_cap import EvalCap

        evaluator.eval_cap = EvalCap(
            max_iterations=10,
            max_output_tokens=75_000,
        )

        captured = []

        def capture_chat(messages, max_tokens, tools=None, temperature=0.1):
            captured.append(max_tokens)
            return LLMResponse(content='{"verdict":"COMPLETE","items":[],"summary":"ok"}')

        with patch.object(llm_client, "chat", capture_chat):
            evaluator.evaluate({"id": "t", "title": "uncorrupted", "criteria": ["x"]})

        assert captured, "chat() was never called"
        # Per-request is always 16384 — session budget of 75000 enforced via EvalCap
        assert captured[0] == 16384, (
            f"Per-request max_tokens should be 16384 (session budget 75000), got {captured[0]}"
        )
        # Verify session budget is correctly stored
        assert evaluator.eval_cap.max_output_tokens == 75_000

    # ── File scope tests ──────────────────────────────────────────

    def test_file_scope_changed_rejects_outside_file(self, evaluator, tmp_workdir):
        """In 'changed' scope, read_file on non-allowed file returns error."""
        evaluator._allowed_files = {"src/main.py", "tests/test_main.py", "pyproject.toml"}

        result = evaluator._tool_read_file("other/unrelated.py")
        assert "error" in result
        assert "File not in scope" in result["error"]
        assert "unrelated.py" in result["error"]

    def test_file_scope_changed_allows_allowed_file(self, evaluator, tmp_workdir):
        """In 'changed' scope, read_file on allowed file succeeds."""
        import os

        os.makedirs(os.path.join(tmp_workdir, "src"), exist_ok=True)
        with open(os.path.join(tmp_workdir, "src", "main.py"), "w") as f:
            f.write("def hello(): pass\n")

        evaluator._allowed_files = {"src/main.py", "tests/test_main.py"}

        result = evaluator._tool_read_file("src/main.py")
        assert "content" in result
        assert "hello" in result["content"]

    def test_file_scope_full_allows_any_file(self, evaluator, tmp_workdir):
        """In 'full' scope (None), read_file on any file succeeds."""
        import os

        os.makedirs(os.path.join(tmp_workdir, "anywhere"), exist_ok=True)
        with open(os.path.join(tmp_workdir, "anywhere", "deep.py"), "w") as f:
            f.write("x=1\n")

        evaluator._allowed_files = None  # full scope

        result = evaluator._tool_read_file("anywhere/deep.py")
        assert "content" in result
        assert "x=1" in result["content"]

    def test_search_pattern_respects_file_scope(self, evaluator, tmp_workdir):
        """In 'changed' scope, search_pattern only finds results in allowed files."""
        import os

        os.makedirs(os.path.join(tmp_workdir, "src"), exist_ok=True)
        os.makedirs(os.path.join(tmp_workdir, "vendor"), exist_ok=True)
        with open(os.path.join(tmp_workdir, "src", "main.py"), "w") as f:
            f.write("TODO: implement\n")
        with open(os.path.join(tmp_workdir, "vendor", "lib.py"), "w") as f:
            f.write("TODO: fix this\n")

        evaluator._allowed_files = {"src/main.py", "Makefile"}

        result = evaluator._tool_search_pattern("TODO")
        assert result["count"] == 1, (
            f"Expected 1 match in allowed files, got {result['count']}: {result['matches']}"
        )
        assert "src/main.py" in str(result["matches"])
        assert "vendor/lib.py" not in str(result["matches"])

    def test_file_scope_integration_with_evaluate(self, evaluator, llm_client, tmp_workdir):
        """End-to-end: evaluator with 'changed' scope works correctly."""
        import os
        import yaml
        import subprocess
        from engine.llm import LLMResponse, ToolCall

        cdir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "config.yaml"), "w") as f:
            yaml.dump({"evaluator": {"file_scope": "changed"}}, f)

        os.makedirs(os.path.join(tmp_workdir, "src"), exist_ok=True)
        with open(os.path.join(tmp_workdir, "src", "main.py"), "w") as f:
            f.write("print('ok')\n")
        subprocess.run(["git", "init"], cwd=tmp_workdir, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_workdir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_workdir, capture_output=True)
        with open(os.path.join(tmp_workdir, "src", "main.py"), "a") as f:
            f.write("# change\n")

        captured = []

        def fake_chat(messages, tools=None, max_tokens=None):
            captured.append(len(messages))
            if len(captured) == 1:
                return LLMResponse(
                    content="checking",
                    tool_calls=[
                        ToolCall(
                            id="tc1", name="read_file", arguments={"path": "nonexistent_outside.py"}
                        )
                    ],
                )
            return LLMResponse(content='{"verdict":"COMPLETE","items":[],"summary":"ok"}')

        with patch.object(llm_client, "chat", side_effect=fake_chat):
            verdict = evaluator.evaluate({"id": "t", "title": "scope test", "criteria": ["c0"]})
        assert verdict.verdict == "COMPLETE"

    def test_file_scope_default_is_changed(self, evaluator, tmp_workdir):
        """Without config override, file_scope defaults to 'changed'."""
        import os

        # Ensure no config.yaml exists
        cdir = os.path.join(tmp_workdir, ".gitreins")
        cfg = os.path.join(cdir, "config.yaml")
        if os.path.exists(cfg):
            os.remove(cfg)

        # In a fresh git repo with no changes, _allowed_files will be empty
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_workdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_workdir, capture_output=True
        )

        evaluator._allowed_files = evaluator._compute_allowed_files()
        # In a clean repo with no uncommitted changes, set should be empty (or just config files)
        assert "src/main.py" not in evaluator._allowed_files, (
            "No changed files should be in scope for a clean repo"
        )


class TestDataFileHelpers:
    """Unit tests for the DATA_FILE_PATTERNS matcher and diff filter
    (JUDGE-CONTEXT-001)."""

    @pytest.mark.parametrize(
        "path",
        [
            ".coding-hermes/board/events.jsonl",
            ".coding-hermes/tasks.md",
            "events.jsonl",
            "board/tasks.json",
            "data.parquet",
            "state.db",
            "db.sqlite",
            "backup.bak",
            "server.log",
            "bin/tool",
            "sub/bin/tool",
            "img.png",
            "photo.jpg",
            "photo.jpeg",
            "doc.pdf",
            ".git/index",
        ],
    )
    def test_data_paths_match(self, path):
        from engine.evaluator import _is_data_file_path

        assert _is_data_file_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "engine/evaluator.py",
            "main.go",
            "src/index.ts",
            "schema.sql",
            "README.md",
            "binary_search.py",  # 'bin' substring is not 'bin/' dir
            "data.jsonnet",  # suffix must match exactly
        ],
    )
    def test_code_paths_do_not_match(self, path):
        from engine.evaluator import _is_data_file_path

        assert _is_data_file_path(path) is False

    def test_filter_diff_text_drops_data_sections(self):
        from engine.evaluator import _filter_diff_text

        diff = (
            "diff --git a/main.py b/main.py\n"
            "index 111..222 100644\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/.coding-hermes/board/events.jsonl b/.coding-hermes/board/events.jsonl\n"
            "index 333..444 100644\n"
            "--- a/.coding-hermes/board/events.jsonl\n"
            "+++ b/.coding-hermes/board/events.jsonl\n"
            "@@ -1 +1 @@\n"
            '-{"event": "SECRET", "pad": "xxxx"}\n'
            '+{"event": "SECRET2", "pad": "yyyy"}\n'
        )
        out = _filter_diff_text(diff)
        assert "main.py" in out
        assert "+new" in out
        assert "events.jsonl" not in out
        assert "SECRET" not in out

    def test_filter_diff_text_empty(self):
        from engine.evaluator import _filter_diff_text

        assert _filter_diff_text("") == ""
